# Customer 360 Capstone — Process Doc

Progress log for the Customer 360 (Databricks Apps + Lakebase) capstone. Presentation-ready.

---

## What we're building
A "Customer 360" web app for synthetic **Acme Retail** (10k customers): browse/filter customers,
360° detail view, notes + segment overrides, Genie chat, embedded AI/BI dashboard, and a
forward-ETL trigger. **Backend:** FastAPI + psycopg + Databricks SDK. **Frontend:** React + Vite +
TypeScript. **Deploy:** Databricks App via DABs.

## Architecture in one picture
```
  Delta gold  ──reverse ETL (synced tables, T1)──▶  Lakebase (Postgres)  ──reads──▶  FastAPI + React app
 (source of truth)                                  synced = fast reads             (SP for Lakebase,
      ▲                                             staging = app writes             OBO for warehouse/Genie)
      └──────── forward ETL (MERGE, T7) ◀────────── staging tables ◀──writes──────────────┘
```
- **Lakebase (Postgres)** = fast operational store for the app. **Synced tables** mirror gold (read);
  **staging tables** capture app writes (notes/overrides).
- **Two identities:** app SP for all Lakebase access; user OBO for SQL warehouse + Genie.

---

## Environment
| Item | Value |
|---|---|
| Workspace | Azure Field Eng East (`adb-984752964297111.11`) |
| Gold data | `ai_27.lakebase_apps_capstone_gold` (5 tables, 10k customers) |
| Lakebase instance | `ai27-lb-apps-capstone` / db `cust360ai27` / UC catalog `ai27_lb_apps_capstone` |
| SQL warehouse | Shared Endpoint |
| App | `customer360` — https://customer360-984752964297111.11.azure.databricksapps.com |
| Repo | github.com/abhishekiyer-databricks/cust360-capstone |
| CLI | `databricks bundle deploy --target dev` / `bundle run` from repo root |

---

## Progress

### ✅ Setup — provisioning
Ran the repo installer: created gold tables, Lakebase instance, AI/BI dashboard, Genie space.
Reconstructed `app/.env` (installer's local scaffold step had failed on a `/Workspace` path).

### ✅ T1 — Reverse ETL: synced + staging tables
Authored `lakebase/reverse_etl/` scripts (committable, idempotent) and ran them:
- **3 synced tables:** `customers_synced` + `transactions_synced` (**CONTINUOUS**),
  `products_synced` (**TRIGGERED**, slow-changing catalog). `customers_synced` = 10,000 rows.
- **3 staging tables:** `customer_notes_staging`, `customer_segment_overrides_staging`,
  `customer_audit_log` (with `processed` flags + audit).
- **App SP grants:** SELECT on synced, SELECT/INSERT/UPDATE on staging — verified.

### ✅ App skeleton
Minimal FastAPI hello app deployed as a **DABs app** → created the app + its **service principal**
(needed for the T1 grants). App is **RUNNING**; `/api/health` returns HTTP 200.

### ✅ T2 — Auth: OBO + SP clients + `lakebase_sp()`
Verified on the deployed app: `/api/whoami` → `abhishek.iyer@databricks.com` (OBO = calling user);
`/api/whoami-sp` → SP `10c64b22-…` id `144899163311454`; `/api/db-check` → `{"select_1":1}`.

### ✅ T3 — App APIs + React UI
Built as **three deployable slices**, each verified end-to-end against live data. Also stood up the
**entire React frontend** (was empty): Vite + TS + TanStack Query + **Mantine** (Databricks-themed
`AppShell` — sidebar, top bar with user email, floating Genie stub), code-split routes.

- **3A — Read path (Lakebase synced, app SP):**
  - `GET /api/customers` — server-side paginated + filtered (segment / min-LTV / max-churn) list from
    `customers_synced`. Envelope `{items,total,page,page_size}`, default 25, **hard cap 100 → 422**
    (never ships 10k rows). Segment filter is case-insensitive `ILIKE`.
  - `GET /api/customers/{id}` — profile + last-20 transactions (two queries, no N+1).
  - React: `Customers` list (mantine-datatable, debounced filters, row → detail) + `CustomerDetail`
    (Profile + Activity tabs).
- **3B — Metrics (SQL warehouse + OBO):**
  - `GET /api/customers/{id}/metrics` — cross-table aggregate on **gold via the warehouse as the
    calling user (OBO)**: lifetime / 30-day / 90-day spend, top-5 categories, open tickets, avg CSAT.
  - `GET /api/segments` — 8 named segments (`customer_segments`, gold), TTL-cached ~5m.
  - React: Metrics tab (stat cards + category bars, own query, 60s cache); segment filter upgraded to a
    **named dropdown**.
  - **Gotcha (documented for reflection):** OBO consent is captured per-user at first authorization and
    does **not** auto-widen when a scope is added. Adding `sql` mid-build meant the developer (who
    authorized in T2) had to **re-consent** (incognito / clear cookies for both the app + workspace
    domains). New users are unaffected — first login consents to the full current scope set.
- **3C — Write path (Lakebase staging, app SP, transactional + audited):**
  - `POST /api/customers/{id}/notes` — INSERT note **+ `customer_audit_log` row in one transaction**;
    actor from `X-Forwarded-Email` (400 if absent, 422 on empty).
  - `POST /api/customers/{id}/segment` — **idempotent** UPSERT (`ON CONFLICT (customer_id) … WHERE value
    changed`): a real change updates the single row + writes one audit entry; re-submitting the same
    value is a no-op (no duplicate row, no audit).
  - `GET /api/customers/{id}/notes` — notes list.
  - React: Notes tab (add form + live list, invalidate-on-write) + Segment tab (current + override form,
    "changed vs no-op" toast).
  - **Verified against live Lakebase:** audit row written in the same txn for every write; same-value
    override → `changed:false`, zero audit rows, exactly one override row. Test data cleaned up.

**Best practices applied:** Pydantic response models + minimal payloads, GZip, per-request
`X-Request-Id`, slow-query logging, parameterized SQL everywhere, TanStack Query per-key staleTimes
(list 10s / detail 30s / metrics 60s / segments 5m) + invalidate-after-write, debounced filters,
code-split routes.
