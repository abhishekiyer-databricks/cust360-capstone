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

### ✅ T7 — Forward ETL: Lakebase staging → Delta gold
Closes the write loop: notes/overrides the app stages in Lakebase are promoted back into Delta gold,
on demand, from the **Reports** page. Built as **two slices**, both verified end-to-end.

**Pattern A chosen (psycopg + Spark `MERGE INTO`), not Pattern B.** We re-checked the "native"
alternative: the task doc calls it *Lakehouse Sync (Beta)*, but that label is stale — it's now
**Lakebase Change Data Feed (CDF)**, **Public Preview** (not GA). We deliberately chose Pattern A
anyway because: (1) CDF is still preview; (2) its replication is **continuous-only** (no on-demand
trigger/flush API) so the "Run forward-ETL" button maps poorly to it; (3) it still needs a consumer
job to dedup `lb_*_history` into gold, so it doesn't remove the work; (4) Pattern A makes the
read→MERGE→mark loop + idempotency explicit and forces creating `gold.customer_notes` — the write
path we want to demonstrate. *Reflection point: deliberate engineering trade-off, knowing the native
feature exists; would revisit CDF for high-volume streaming / SCD2 audit needs, once GA.*

- **7A — the ETL job:**
  - `lakebase/forward_etl/pattern_a_psycopg2/forward_etl_merge.py` (serverless job): reads
    `*_staging WHERE processed=false` over psycopg → builds Spark DataFrames → `MERGE INTO` gold →
    marks those rows `processed=true`. Notes → **new** `gold.customer_notes` (MERGE on `note_id`,
    INSERT-only; **table created by the job**, `CREATE TABLE IF NOT EXISTS`); overrides → `UPDATE
    gold.customers.segment_id` (MERGE on `customer_id`). Audit log stays in Lakebase.
  - `resources/jobs.yml` — DABs job `customer360_forward_etl`, app SP granted `CAN_MANAGE_RUN`.
  - **Key correctness point (reflection):** MERGE (Delta) and the `processed` flag (Postgres) are in
    **two systems — no single cross-system transaction.** Order = **merge first, mark second**;
    idempotency comes from MERGE-on-key + the `processed` filter, not a 2-phase commit. Proven live:
    the first run crashed at mark-processed (a UUID cast bug) *after* the MERGEs — gold still ended
    with exactly the right rows, and the re-run flagged them with **zero duplication**.
- **7B — app wiring + Reports page:**
  - `app/backend/routers/jobs.py` (all as the **app SP**, not OBO): `POST /api/jobs/run-forward-etl`
    (`jobs.run_now`), `GET /api/jobs/{run_id}` (poll), `GET /api/jobs/runs` (history). 503 if job id
    unconfigured, 502 on SDK failure. `FORWARD_ETL_JOB_ID` via `app.yaml` env (→ `valueFrom` in T6).
  - `Reports.tsx`: "Run forward-ETL" button + live status badge (polls every 3s, stops at terminal)
    + recent-runs table with "Open in workspace" deep links.

**Verified end-to-end on the deployed app** (notes + 5 segment overrides added via the UI, then run
from the Reports button): `gold.customer_notes` = 5 rows / 5 distinct note_ids; all 5 overrides
propagated to `gold.customers.segment_id`; staging flags flipped to `processed=true` (proving the SP
`CAN_MANAGE_RUN` trigger path); **re-run with nothing new = no-op** (`notes_merged:0, overrides_merged:0`).

**Portability / packaging notes (for T8):** `jobs.yml` references the app SP as
`${resources.apps.customer360.service_principal_client_id}` (DABs output, resolved at deploy time) —
no hardcoded UUID, portable to any workspace, engine-agnostic. Deferred to T8: switch the job's
**prod** `run_as` to the app SP (+ grant it `USE CATALOG/SCHEMA` + `MODIFY` on gold via a bundle
`grants` block), and note the **direct deployment engine** (no-Terraform; migrate with
`bundle deployment migrate`, CLI ≥ 0.279.0; caveat — removing a YAML field reverts it to default).

### ✅ T4 — Embed the AI/BI dashboard
Reps see broader analytics in-app via an `<iframe>` — the supported embed pattern; the dashboard
authenticates the *viewer* itself, so there's no data/API plumbing.
- `/api/config` gained `databricks_host`; `Dashboard.tsx` renders `${host}/embed/dashboardsv3/${dashboard_id}`.
- **Gotcha (root-caused live):** the Apps runtime injects `DATABRICKS_HOST` **without a scheme**
  (`adb-….net`, unlike `.env` which has `https://`). A scheme-less host makes the iframe `src` a
  **relative** URL → it resolves against the app's own origin → our SPA catch-all serves `index.html`
  → **the dashboard iframe loaded the whole app again, nesting infinitely.** Fix: normalize to
  `https://{host}` in `/api/config` (+ defensively in the frontend). *Reflection point.*
- Workspace embed allowlist already covered our host via `*.databricksapps.com` (matches at any
  sub-label depth) — no action needed. **Verified: dashboard renders in-app with data.**

### ✅ T5 — Genie chat (floating overlay, OBO)
Natural-language Q&A over gold, as the **calling user** (OBO), so Genie's own governance/audit apply.
- `routers/genie.py` — 3 OBO endpoints wrapping the async **Conversation API**: start conversation /
  create follow-up message / poll `get_message`; on `COMPLETED` with a query attachment, fetch the
  attachment's result rows (capped at 50 for the preview). No token → 401; `PermissionDenied` → 403;
  SDK error → 502. *(SDK surface verified against installed v0.122.0 before coding — `start_conversation`
  / `create_message` return `Wait[GenieMessage]`, `.response` = initial message.)*
- **Backend is stateless; the frontend owns `conversation_id` + `message_id`** and drives the poll loop
  — reusing `conversation_id` is what preserves context. Each GET is one `get_message` call, so no
  request hangs.
- `GenieWidget.tsx` — floating bottom-right launcher → chat panel: client poll loop (1.5s interval,
  **~30s cap** → friendly timeout), typing indicator, **Enlarge** toggle, **"Open in workspace"** deep
  link (`${host}/genie/rooms/${space_id}`), result-preview table with readable number formatting
  (Genie returns values as strings, sometimes in scientific notation), and SQL behind a default-closed
  **"Show SQL"** disclosure. A header **"new chat"** control resets to a fresh thread (confirm popover
  when messages exist).
- **Design note (reflection):** Genie conversations are **ephemeral by our choice** — held in React
  state, single-threaded, wiped on refresh; "New chat" starts fresh and "Open in workspace" is the
  history escape hatch. The `conversation_id`s *are* durable server-side (SDK `list_conversations` etc.),
  so persistence (localStorage or a Lakebase per-user table) is a straightforward future add, not a
  platform limit.
- **Verified end-to-end against the live Genie space:** "top segment by LTV" → **Champions, $20.38M**
  with a result preview + generated SQL; follow-up ("top 10 customers in this segment") correctly kept
  context. No-token → 401.
- **UI gotcha (root-caused live):** the "new chat" confirm popover appeared to do nothing — Mantine
  `Popover` portals at default `zIndex: 300`, below the chat panel's `zIndex: 1000`, so it rendered
  *behind* the panel. Fix: `zIndex={1100}` on the popover.

### ✅ T6 — App configuration finalize (`app.yaml`)
Because OBO shipped in T2, T6 was **finalize + verify**, not net-new. Three things closed:
- **Secret `valueFrom` bindings.** Lakebase connection vars (`PGHOST`/`PGDATABASE`/`PG_INSTANCE_NAME`)
  now resolve from the `capstone-abhishek-iyer` secret scope (keys `pg_host`/`pg_database`/
  `pg_instance_name`) via `valueFrom` — declared as `resources:` on the app in `resources/app.yml`,
  referenced by key in `app.yaml`. The runtime resolves a secret binding to the decrypted value.
- **Job-id resource binding.** `FORWARD_ETL_JOB_ID` was a hardcoded magic number with a `TODO(T6)`;
  now `valueFrom` a `job:` resource bound to the bundle-managed `forward_etl` job
  (`id: ${resources.jobs.forward_etl.id}`) — resolves to the real id at deploy, portable across
  workspaces.
- **Scope-location call.** OBO scopes stay in `resources/app.yml` `user_api_scopes` (`sql`,
  `dashboards.genie`), *not* an `app.yaml` `user_authorization` block as the task doc describes —
  under DABs the app-resource field is authoritative and the two would conflict. Documented in both files.
- **Secret-vs-config split (reflection):** we bind Postgres *connection* details (arguably sensitive,
  and in the scope) via `valueFrom`, but leave public identifiers (warehouse/dashboard/Genie/catalog/
  schema) as plain `value:` — they're not credentials. A deliberate line, not laziness.
- `bundle validate` passes; secret bindings resolve fully in the compiled bundle (the job `id` stays a
  `${…}` reference since a job id is a deploy-time computed output). T6's done-when — *app starts with
  no missing-secret errors* + *OBO reaches SQL + Lakebase + Genie without 401s* — is **exercised by
  deploying T4 + T5** (dashboard renders, Genie answers, metrics/writes still work).
