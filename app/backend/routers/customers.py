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

from fastapi import APIRouter, HTTPException, Query
from psycopg.rows import dict_row

from ..db import lakebase_sp
from ..models import Customer, CustomerDetail, CustomerProfile, Page, Transaction

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


def _log_slow(label: str, started: float) -> None:
    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms > _SLOW_MS:
        log.warning("slow query %s took %.0fms", label, elapsed_ms)


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
    _log_slow("list_customers", started)

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
    _log_slow("get_customer", started)

    return CustomerDetail(
        profile=CustomerProfile(**profile_row),
        recent_transactions=[Transaction(**r) for r in txn_rows],
    )
