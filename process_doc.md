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
| CLI | **use Homebrew `databricks` v1.5.0** for bundle commands |

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
