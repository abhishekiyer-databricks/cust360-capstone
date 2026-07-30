"""Customer read endpoints (T3, slice 3A) — Lakebase synced tables via the app SP.

These are the *fast operational reads* (master_plan §2): point/paged reads served from
Lakebase synced tables (`customers_synced`, `transactions_synced`), which are the CONTINUOUS
mirrors of gold from T1. All Lakebase access is as the **app service principal**
(`lakebase_sp()`), never OBO — Lakebase doesn't support OBO scopes (master_plan §3-D2).

Endpoints:
- GET /api/customers           — paginated, filtered list (D1: envelope, cap 100)
- GET /api/customers/{id}      — profile + last 20 transactions (two queries, no N+1)

The Metrics tab (warehouse + OBO) and the write path (staging + audit) are added in slices
3B and 3C respectively.
"""
from __future__ import annotations

import logging
import time

from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..auth import caller_email, obo_client
from ..db import lakebase_sp
from ..models import (
    CategorySpend,
    Customer,
    CustomerDetail,
    CustomerMetrics,
    CustomerProfile,
    Note,
    NoteCreate,
    Page,
    Segment,
    SegmentOverrideCreate,
    SegmentOverrideResult,
    Transaction,
)
from ..warehouse import run_query


def _require_actor(request: Request) -> str:
    """The human behind an audited write, from X-Forwarded-Email. 400 if absent.

    Lakebase runs as the app SP (no per-user DB identity), so we attribute writes to the
    calling user via this header. Behind the Apps proxy it's always present; a missing value
    means the endpoint was reached outside the proxy — reject rather than write a null actor.
    """
    email = caller_email(request)
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Missing X-Forwarded-Email — audited writes require a known actor.",
        )
    return email

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["customers"])

# Columns pulled for the list row — minimal set (D4 / master_plan §7: no SELECT *).
_LIST_COLS = (
    "customer_id, first_name, last_name, email, country, "
    "segment_id, lifetime_value, churn_score"
)
# Full profile columns for the detail page.
_PROFILE_COLS = (
    "customer_id, first_name, last_name, email, phone, country, city, gender, age, "
    "signup_date, last_purchase_date, segment_id, lifetime_value, churn_score, updated_at"
)

_SLOW_MS = 500  # log queries slower than this at WARNING (master_plan §7 observability)


def _log_slow(label: str, started: float, params: object = None) -> None:
    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms > _SLOW_MS:
        # Structured extras (O4): params + elapsed land as fields in the JSON log line.
        log.warning(
            "slow query %s took %.0fms",
            label,
            elapsed_ms,
            extra={"query": label, "elapsed_ms": round(elapsed_ms), "params": params},
        )


@router.get("/customers", response_model=Page[Customer])
def list_customers(
    segment: str | None = Query(None, description="Filter by segment_id (e.g. S1)"),
    min_ltv: float | None = Query(None, ge=0, description="Minimum lifetime_value"),
    max_churn: float | None = Query(None, ge=0, le=1, description="Maximum churn_score"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100, description="Rows per page (max 100)"),
) -> Page[Customer]:
    """Paginated + filtered customer list from `customers_synced` (Lakebase, app SP).

    Server-side pagination (D1): OFFSET/LIMIT + a COUNT(*) with the same filter. `page_size`
    is hard-capped at 100 by the Query() validator, so oversized requests get a 422 — we never
    return all 10k rows in one response.
    """
    # Build the shared WHERE clause from the optional filters. Parameterized (never string
    # interpolation) to avoid injection and get plan reuse.
    where: list[str] = []
    params: list[object] = []
    if segment:
        # Case-insensitive "contains" so typing `S` matches S1..S7 (forgiving free-text UX).
        # 3B replaces this box with a proper dropdown of the 7 named segments (D7).
        where.append("segment_id ILIKE %s")
        params.append(f"%{segment}%")
    if min_ltv is not None:
        where.append("lifetime_value >= %s")
        params.append(min_ltv)
    if max_churn is not None:
        where.append("churn_score <= %s")
        params.append(max_churn)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    offset = (page - 1) * page_size
    started = time.monotonic()
    with lakebase_sp() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM customers_synced {where_sql}", params)
            total = cur.fetchone()["n"]

            # Stable ORDER BY so pagination is deterministic; sort by LTV desc then id.
            cur.execute(
                f"SELECT {_LIST_COLS} FROM customers_synced {where_sql} "
                "ORDER BY lifetime_value DESC NULLS LAST, customer_id "
                "LIMIT %s OFFSET %s",
                [*params, page_size, offset],
            )
            rows = cur.fetchall()
    _log_slow("list_customers", started, params={"where": where, "values": params})

    items = [Customer(**row) for row in rows]
    return Page(items=items, total=total, page=page, page_size=page_size)


@router.get("/customers/{customer_id}", response_model=CustomerDetail)
def get_customer(customer_id: str) -> CustomerDetail:
    """Profile + last 20 transactions from Lakebase (app SP). Two queries — no N+1."""
    started = time.monotonic()
    with lakebase_sp() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_PROFILE_COLS} FROM customers_synced WHERE customer_id = %s",
                [customer_id],
            )
            profile_row = cur.fetchone()
            if profile_row is None:
                raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")

            cur.execute(
                "SELECT transaction_id, product_id, transaction_date, channel, status, amount "
                "FROM transactions_synced WHERE customer_id = %s "
                "ORDER BY transaction_date DESC NULLS LAST, transaction_id DESC LIMIT 20",
                [customer_id],
            )
            txn_rows = cur.fetchall()
    _log_slow("get_customer", started, params={"customer_id": customer_id})

    return CustomerDetail(
        profile=CustomerProfile(**profile_row),
        recent_transactions=[Transaction(**r) for r in txn_rows],
    )


# ---------------------------------------------------------------------------
# Metrics — cross-table aggregate on gold via the SQL warehouse, as the USER (OBO).
# This is the one read that does NOT use Lakebase: it joins transactions × products ×
# support_tickets (support_tickets isn't synced), so the warehouse is the right engine and
# OBO makes the warehouse audit reflect the calling user (master_plan §2 / §3-D2).
# ---------------------------------------------------------------------------
# Only completed sales count toward spend (pending/refunded excluded — verified against data).
_METRICS_SQL = """
WITH tx AS (
  SELECT t.amount, t.transaction_date, p.category
  FROM transactions t
  LEFT JOIN products p ON t.product_id = p.product_id
  WHERE t.customer_id = :customer_id AND t.status = 'completed'
)
SELECT
  (SELECT coalesce(sum(amount), 0) FROM tx) AS lifetime_spend,
  (SELECT coalesce(sum(amount), 0) FROM tx
     WHERE transaction_date >= current_date - INTERVAL 30 DAYS) AS spend_30d,
  (SELECT coalesce(sum(amount), 0) FROM tx
     WHERE transaction_date >= current_date - INTERVAL 90 DAYS) AS spend_90d,
  (SELECT count(*) FROM support_tickets
     WHERE customer_id = :customer_id AND status IN ('open', 'in_progress')) AS open_tickets,
  (SELECT round(avg(csat_score), 2) FROM support_tickets
     WHERE customer_id = :customer_id) AS avg_csat
"""

_TOP_CATEGORIES_SQL = """
SELECT p.category AS category, round(sum(t.amount), 2) AS amount
FROM transactions t
LEFT JOIN products p ON t.product_id = p.product_id
WHERE t.customer_id = :customer_id AND t.status = 'completed'
GROUP BY p.category
ORDER BY amount DESC
LIMIT 5
"""


@router.get("/customers/{customer_id}/metrics", response_model=CustomerMetrics)
def get_customer_metrics(customer_id: str, request: Request) -> CustomerMetrics:
    """Live cross-table metrics from gold via the SQL warehouse (OBO — the calling user)."""
    ws = obo_client(request)  # 401 if no proxy token — never falls back to the SP
    params = {"customer_id": customer_id}

    agg_rows = run_query(ws, _METRICS_SQL, params, label="metrics")
    agg = agg_rows[0] if agg_rows else {}
    cat_rows = run_query(ws, _TOP_CATEGORIES_SQL, params, label="metrics_top_categories")

    def num(v) -> float:
        return float(v) if v is not None else 0.0

    return CustomerMetrics(
        lifetime_spend=num(agg.get("lifetime_spend")),
        spend_30d=num(agg.get("spend_30d")),
        spend_90d=num(agg.get("spend_90d")),
        top_categories=[
            CategorySpend(category=r.get("category"), amount=num(r.get("amount"))) for r in cat_rows
        ],
        open_tickets=int(agg.get("open_tickets") or 0),
        avg_csat=float(agg["avg_csat"]) if agg.get("avg_csat") is not None else None,
    )


# ---------------------------------------------------------------------------
# Segments — the named segment list for the filter dropdown + Segment display.
# Small, slow-changing reference data (8 rows): fetch once via the warehouse (OBO) and cache
# server-side ~5m (master_plan §7 caching). Lives in gold (customer_segments), not synced.
# ---------------------------------------------------------------------------
_segments_cache: TTLCache = TTLCache(maxsize=1, ttl=300)


# Idempotent reference data → browser may cache 5m (Optimizations O2). `private` because the
# app is behind per-user auth; matches the client-side staleTime + the server TTLCache.
_REFERENCE_CACHE_CONTROL = "private, max-age=300, must-revalidate"


@router.get("/segments", response_model=list[Segment])
def list_segments(request: Request, response: Response) -> list[Segment]:
    """The 8 named segments (id + name), TTL-cached ~5m."""
    response.headers["Cache-Control"] = _REFERENCE_CACHE_CONTROL
    cached = _segments_cache.get("all")
    if cached is not None:
        return cached

    ws = obo_client(request)
    rows = run_query(
        ws,
        "SELECT segment_id, segment_name FROM customer_segments ORDER BY segment_id",
        label="segments",
    )
    segments = [Segment(segment_id=r["segment_id"], segment_name=r.get("segment_name")) for r in rows]
    _segments_cache["all"] = segments
    return segments


# ---------------------------------------------------------------------------
# Writes (T3C) — notes + segment overrides. All via the app SP (lakebase_sp), each paired
# with a customer_audit_log row in the SAME transaction (master_plan §7 transactional
# integrity). Actor = X-Forwarded-Email. lakebase_sp() yields a non-autocommit connection,
# so the two INSERTs + commit() are atomic; any exception rolls back on context exit.
# ---------------------------------------------------------------------------
@router.get("/customers/{customer_id}/notes", response_model=list[Note])
def list_notes(customer_id: str) -> list[Note]:
    """Notes for a customer, newest first (Lakebase staging, app SP)."""
    with lakebase_sp() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT note_id, customer_id, author_email, note_text, created_at "
                "FROM customer_notes_staging WHERE customer_id = %s "
                "ORDER BY created_at DESC LIMIT 100",
                [customer_id],
            )
            rows = cur.fetchall()
    return [Note(note_id=str(r["note_id"]), **{k: r[k] for k in ("customer_id", "author_email", "note_text", "created_at")}) for r in rows]


@router.post("/customers/{customer_id}/notes", response_model=Note, status_code=201)
def add_note(customer_id: str, body: NoteCreate, request: Request) -> Note:
    """INSERT a note AND append an audit row in one transaction (app SP)."""
    actor = _require_actor(request)
    with lakebase_sp() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "INSERT INTO customer_notes_staging (customer_id, author_email, note_text) "
                    "VALUES (%s, %s, %s) "
                    "RETURNING note_id, customer_id, author_email, note_text, created_at",
                    [customer_id, actor, body.note_text],
                )
                note = cur.fetchone()
                cur.execute(
                    "INSERT INTO customer_audit_log (customer_id, action, actor_email, payload) "
                    "VALUES (%s, %s, %s, %s)",
                    [
                        customer_id,
                        "add_note",
                        actor,
                        Jsonb({"note_id": str(note["note_id"]), "note_text": body.note_text}),
                    ],
                )
            conn.commit()  # both rows or neither
        except Exception:
            conn.rollback()
            raise
    return Note(note_id=str(note["note_id"]), **{k: note[k] for k in ("customer_id", "author_email", "note_text", "created_at")})


@router.post("/customers/{customer_id}/segment", response_model=SegmentOverrideResult)
def override_segment(customer_id: str, body: SegmentOverrideCreate, request: Request) -> SegmentOverrideResult:
    """Idempotent UPSERT of a segment override + conditional audit, in one transaction.

    Re-submitting the SAME override_segment is a no-op (the `ON CONFLICT ... WHERE value
    changed` guard means no row is written and no audit is logged). A real change updates the
    single row (UNIQUE customer_id) and writes one audit entry.
    """
    actor = _require_actor(request)
    with lakebase_sp() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                # INSERT-or-UPDATE keyed on UNIQUE(customer_id). The WHERE on DO UPDATE makes a
                # same-value resubmit affect 0 rows → idempotent (rowcount==0, no dup, no audit).
                cur.execute(
                    "INSERT INTO customer_segment_overrides_staging "
                    "  (customer_id, override_segment, reason, author_email) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (customer_id) DO UPDATE SET "
                    "  override_segment = EXCLUDED.override_segment, "
                    "  reason = EXCLUDED.reason, author_email = EXCLUDED.author_email, "
                    "  created_at = NOW(), processed = FALSE, processed_at = NULL "
                    "WHERE customer_segment_overrides_staging.override_segment "
                    "      IS DISTINCT FROM EXCLUDED.override_segment",
                    [customer_id, body.override_segment, body.reason, actor],
                )
                changed = cur.rowcount > 0
                if changed:
                    cur.execute(
                        "INSERT INTO customer_audit_log (customer_id, action, actor_email, payload) "
                        "VALUES (%s, %s, %s, %s)",
                        [
                            customer_id,
                            "override_segment",
                            actor,
                            Jsonb({"override_segment": body.override_segment, "reason": body.reason}),
                        ],
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return SegmentOverrideResult(
        customer_id=customer_id, override_segment=body.override_segment, changed=changed
    )
