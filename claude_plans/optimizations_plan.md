# Optimizations Plan — Customer 360 (master_plan §5 row 14, §7)

> **Status:** planned (2026-07-30). All tasks **T1–T9 DONE**. This is the hardening pass
> before submission (row 15). Same doc style as the `tN_plan.md` set: concept → decisions →
> step-by-step → deploy/test → done-when. We deploy + test on the workspace (D6) as usual;
> PROD is the submission source of truth (T8), so final verification runs against the
> **prod** app (`bundle deploy -t prod` → `bundle run customer360 --target prod`, GOTCHA #13).
>
> **Reminder (GOTCHA #14):** use `/opt/homebrew/bin/databricks` (v1.5.0) for ALL bundle commands.

---

## 0. Why this pass exists (concept)

The app is feature-complete and correct. Most of `master_plan §7` was baked in as we built
(pagination, GZip, Pydantic models, TanStack caching, debounce, code-split, one-txn writes,
idempotent UPSERT, warehouse timeout, X-Request-Id). This pass closes the remaining §7 items
that were **deliberately deferred** or **never wired**, and produces the **measured perf
numbers + reflection paragraph** the submission checklist (§8) still owes.

The single biggest item is **connection pooling** — `db.py` opens one short-lived psycopg
connection *per request* today. That's correct (fresh token every time) but pays a full TLS
handshake + `generate_database_credential` round-trip on every call. Under any real
concurrency this dominates latency. `psycopg[pool]` is already a dependency; the design was
explicitly deferred to this pass (see `db.py` docstring + master_plan §3-D4).

---

## 1. Scored audit (what's done vs pending)

| §7 item | State | Where |
|---|---|---|
| Pagination (envelope, cap 100→422) | ✅ done | `routers/customers.py` |
| GZip, minimal payloads, Pydantic models | ✅ done | `main.py`, `customers.py`, `models.py` |
| Client-side TanStack staleTimes + invalidate | ✅ done | `Customers.tsx`, `CustomerDetail.tsx` |
| `React.lazy` + Suspense code-split | ✅ done | `App.tsx` |
| Debounced filters (250ms) | ✅ done | `Customers.tsx` |
| Server TTLCache (segments) | ✅ done | `customers.py` |
| X-Request-Id middleware | ✅ done | `main.py` |
| One-txn writes + idempotent UPSERT | ✅ done | `customers.py` |
| Warehouse `wait_timeout=30s` | ✅ done | `warehouse.py` |
| **Connection pooling (`psycopg_pool` + token rotation)** | ❌ **O1** | `db.py` |
| **`Cache-Control` on idempotent GETs** | ❌ **O2** | `main.py` / routers |
| **Lakebase `connect_timeout` + `statement_timeout`** | ❌ **O3** | `db.py` |
| **Structured JSON logging** | ❌ **O4** | `main.py` (new `logging_config.py`) |
| **Slow-query WARNING *with params*** | ⚠️ partial → **O4** | `customers.py`, `warehouse.py` |
| **Composite index `(segment_id, lifetime_value DESC)`** | ❌ **O5** (feasibility) | `lakebase/reverse_etl` |
| **List-grid memoization** | ⚠️ minor → **O6** | `Customers.tsx`, `CustomerDetail.tsx` |
| **Perf verification + reflection writeup** | ❌ **O7** | `process_doc.md`, new `optim/` output |

Keyset pagination ("once large") is intentionally **not** in scope: 10k rows with the O5
index makes OFFSET/LIMIT comfortably meet the <200ms target. Documented as a future item.

---

## 2. Design decisions (decide once)

- **DO1 — Pool shape.** One module-level `psycopg_pool.ConnectionPool`, `min_size=2`,
  `max_size=10` (D4). Token rotation via a **`connection_factory`/`configure`-per-connection**
  that mints a fresh Lakebase credential when a *physical* connection is (re)opened, plus
  `max_lifetime` set **below the ~1h token TTL** (use `max_lifetime=1800` = 30 min) so a
  pooled connection is recycled — and re-authed — well before its token expires. This is
  option (a) from master_plan §3-D4: fresh token per physical connect, not per checkout.
  Keep `lakebase_sp()` as the **same context-manager API** so no router changes are needed —
  it just checks out of the pool instead of dialing a new socket. Add a FastAPI `lifespan`
  to `open()` the pool on startup and `close()` on shutdown.
  - **Risk to handle:** if the app SP's PG role/token can't be minted at import time (local),
    the pool must open lazily / tolerate a cold start — guard so `config.py`-style local
    imports don't crash. Mirror the existing "works locally too" posture.

- **DO2 — Cache-Control policy.** Only on **idempotent, non-user-specific GETs**:
  `/api/config` and `/api/segments` → `Cache-Control: private, max-age=300, must-revalidate`.
  Per-customer reads (`/customers`, `/customers/{id}`, `/metrics`, `/notes`) get
  `private, max-age=0, must-revalidate` (or simply no caching) — they're user/session-scoped
  and change on writes. **Never** cache the write endpoints. `private` (not `public`) because
  responses are behind per-user auth.

- **DO3 — Timeouts.** `connect_timeout=10` on the psycopg connection; set
  `statement_timeout` (e.g. `15000` ms) as a session `options` param or via `SET` on
  checkout so a runaway Lakebase query can't pin a worker. Genie stays single-shot
  (frontend owns the poll loop) — no server-side timeout needed there beyond the SDK default.

- **DO4 — Logging.** Add a tiny JSON formatter (stdlib only, no new dep) that emits
  `{ts, level, logger, msg, request_id, ...extra}`. Pull `request_id` from
  `request.state.request_id` where available. Extend slow-query logs to include the
  **bound params** (redact nothing — this is internal synthetic data, but keep values short).

- **DO5 — Index feasibility (the one unknown).** Lakebase `customers_synced` is a
  **pipeline-managed synced table**. Before creating the composite index, verify whether
  the sync pipeline permits secondary indexes on the target and whether a resync would drop
  it. Two outcomes:
  - **If allowed & durable:** `CREATE INDEX CONCURRENTLY idx_customers_seg_ltv ON
    customers_synced (segment_id, lifetime_value DESC)` in a new
    `lakebase/reverse_etl/04_indexes.py` (idempotent, `IF NOT EXISTS`), and measure the
    list-endpoint gain (mirrors the T9 index methodology: `EXPLAIN (ANALYZE, FORMAT JSON)`
    → server `Execution Time`, not client wall-clock — GOTCHA #17).
  - **If not allowed / gets dropped on resync:** document that clearly as a platform
    constraint (great reflection point), and note the alternative (index the gold Delta side
    / rely on the small row count). Do NOT fight the pipeline.

- **DO6 — React memoization scope.** `useMemo` the DataTable `columns` arrays (they're static)
  and confirm row rendering isn't re-instantiating. This is polish; keep it minimal — don't
  over-memoize primitives.

- **DO7 — Verification is server-side.** Every "done-when" number is measured
  **server-side** (FastAPI timing log / `EXPLAIN ANALYZE`), never laptop→Azure wall-clock
  (the T9 lesson). Capture into `optim/optim_run_output.txt` for the writeup.

---

## 3. Step-by-step implementation

**O1 — Connection pool (`app/backend/db.py` + `main.py`)**
1. Add `_make_pool()` building a `ConnectionPool(conninfo=..., min_size=2, max_size=10,
   max_lifetime=1800, kwargs={connect_timeout:10}, configure=_configure)` where
   `_configure(conn)` sets `statement_timeout` and (if using per-connect tokens) the password
   is minted in a `connection_class`/factory. Simplest robust route: subclass or use
   `check`/`configure` + a custom `connection_factory` that injects `password=_mint_token()`.
2. Rewrite `lakebase_sp()` to `with _pool().connection() as conn: yield conn` (same signature).
3. Add FastAPI `lifespan` in `main.py`: open pool on startup, `pool.close()` on shutdown.
4. Keep the `_mint_token()` helper; it's now called per physical connection, not per request.

**O2 — Cache-Control (`main.py`)**
5. Add a small dependency or middleware that stamps `Cache-Control` per the DO2 policy; or set
   `response.headers["Cache-Control"]` directly in `/api/config` and `/api/segments`.

**O3 — Timeouts (`db.py`)** — folded into O1 pool `kwargs`/`configure` (steps 1).

**O4 — Structured logging (`app/backend/logging_config.py` new + `main.py`)**
6. New `logging_config.py` with a `JsonFormatter(logging.Formatter)`; wire it in `main.py`
   replacing `logging.basicConfig(format=...)`. Add a `logging.Filter` (or contextvar) to
   attach `request_id` from the middleware.
7. Extend `_log_slow(...)` in `customers.py` and the slow branch in `warehouse.py` to log the
   params dict.

**O5 — Index (`lakebase/reverse_etl/04_indexes.py` new)** — per DO5 outcome. Feasibility check
   first (query `pg_indexes`, try a resync note), then create + measure or document constraint.

**O6 — React memo (`Customers.tsx`, `CustomerDetail.tsx`)**
8. `const columns = useMemo(() => [...], [])` for both DataTables.

**O7 — Verification + writeup (`app/backend/optim/` or `lakebase/optim/` + `process_doc.md`)**
9. Small script/notebook: hit `/api/customers` (cold + warm), `/customers/{id}`, run the
   `EXPLAIN ANALYZE` before/after O5, capture pool behavior under a few concurrent requests.
   Save to `optim/optim_run_output.txt`. Write the reflection paragraph (which optimizations
   done + which deferred + why) into `process_doc.md` and tick master_plan §8.

---

## 4. How to deploy & test

- Local: `python -m py_compile` backend; `npm run build` in `app/frontend` (rebuild the
  committed `backend/static` bundle — T8 requires it for prod git-source).
- `bundle validate` → `bundle deploy -t prod` → **`bundle run customer360 --target prod`**
  (GOTCHA #13: deploy alone does not restart; run starts a new serving deployment).
- In an authed browser on the prod app URL:
  - Network tab shows `Cache-Control` on `/api/config` + `/api/segments`; repeat nav = 304/cache.
  - List + detail still correct; writes still land + audit still written.
  - App logs (`databricks apps logs customer360`) show **JSON lines** with `request_id`.
  - Pool: no per-request `generate_database_credential` storm; connections reused.

---

## 5. Done-when checklist

- [x] **O1** `lakebase_sp()` checks out of a `psycopg_pool` (min 2 / max 10); token rotates per
      physical connection (`_FreshTokenConnection`); `max_lifetime=1800s` < ~1h token TTL; pool
      opens/closes with app lifespan; no router changes needed; local import still works
      (`open=False`). **Verified live:** min=2 conns pre-opened + reused, no per-request
      credential mint (pool stats `connections_num=2` across 2 requests).
- [x] **O2** `Cache-Control: private, max-age=300, must-revalidate` present on `/api/config`
      (verified via TestClient) + `/api/segments`; write endpoints uncached.
- [x] **O3** `connect_timeout=10` + `statement_timeout=15000ms` set on Lakebase connections.
      **Verified live:** `SHOW statement_timeout` → `15s`.
- [x] **O4** Logs are structured JSON (`logging_config.JsonFormatter`) with `request_id`
      (contextvar set in middleware); slow-query WARNING includes params + elapsed_ms as JSON
      fields. **Verified:** sample log line emits correct JSON with extras.
- [x] **O5** `lakebase/reverse_etl/04_indexes.py` run as a workspace job. **Result (NEGATIVE,
      documented):** synced tables DO accept a secondary index (`index_supported: true`), but the
      index is **inert** for this query — `before 3.94ms → after 4.49ms`, `plan_after: Seq Scan`.
      Cause: list filter is `segment_id ILIKE '%S1%'` (leading-wildcard → b-tree unusable) + 10k
      rows too small to prefer an index scan. <200ms target already met by Seq Scan. **Index
      dropped** (write cost on a CONTINUOUS synced table, no read benefit). Captured in
      `lakebase/optim/o5_index_output.txt`. Mirror image of the T9 18× win → good reflection.
- [x] **O6** DataTable `columns` memoized (`useMemo`) on both list (`Customers.tsx`) + detail
      (`CustomerDetail.tsx`). tsc clean, `npm run build` clean.
- [x] **O7** Perf verified server-side (list ~4ms Seq Scan << 200ms target; pooling proven — no
      per-request credential mint); reflection section added to `process_doc.md`; O5 output
      captured in `lakebase/optim/o5_index_output.txt`.
- [~] Re-verify on the **prod** app (deploy + `bundle run`) — user step (final gate).

Legend: [x] done + verified · [~] implemented, awaits prod run.

**Perf targets (master_plan §7 "done when"):** list endpoint <200ms server-side (cold, no
warehouse); detail first paint <800ms warm cache; visible TanStack cache hits on tab/back
nav; no N+1 Lakebase queries on detail; **pooling proven** (no per-request credential mint).

---

## 6. Explicitly out of scope (documented as future items)

- **Keyset pagination** — OFFSET/LIMIT + O5 index meets the target at 10k rows.
- **`resources/lakebase.yml`** synced-table DABs management — skipped in T8 (recreate risk);
  the O5 index script sits alongside T1's `reverse_etl/` scripts, run manually.
- **Client-side Genie/notes persistence** — noted in T5 as a straightforward future add.
- **Per-customer server cache** — deliberately not cached (freshness + write invalidation).

---

## 7. After this pass → Submission (master_plan §5 row 15, §8)

Reflection paragraph (sync-mode choices + optimizations done/next), 3-min recording, repo +
live app URLs, `m2m_test.py` stdout, T9 numbers (already captured). The Graphviz architecture
diagram (§9) is the final flourish.
