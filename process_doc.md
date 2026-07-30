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

### ✅ T8 — Deploy as a git-source app (via DABs)
Switched `customer360` from a workspace-folder upload to the **production git-source pattern**:
Databricks now pulls the app source from GitHub (`abhishekiyer-databricks/cust360-capstone` @ `main`,
path `app`) on each `bundle run`, **as the app service principal**.
- **Per-target source (no `source_code_path` in the base).** DABs rejects an app with *both*
  `git_source` and `source_code_path`, so the base `resources/app.yml` carries no source; the source is
  set per-target in `databricks.yml`: **dev** = `source_code_path: ./app` (fast local iteration),
  **prod** = `git_repository` + `git_source`. `databricks.yml` also gained the full `variables:` set
  (warehouse/lakebase/dashboard/genie/catalog/pg_uc_catalog + git repo/branch) and a prod
  `workspace.root_path` (required by `mode: production`).
- **Committed the built React bundle.** Un-ignored `app/backend/static/` and committed it so the runtime
  command stays `uvicorn backend.main:app` with no build step (rebuild + commit before each prod deploy).
- **SP-bound git credential (best practice even though the repo is public).** A git-source app pulls as
  its **service principal**, not the deploying user — a User-Settings *Linked account* credential does
  **not** apply. Registered a GitHub PAT (from the repo-owner account `abhishekiyer-databricks`, email
  `abhishek.iyer@databricks.com`) bound to the app SP via
  `git-credentials create … "principal_id": 144899163311454` (cred id `963031370981151`). No SP
  impersonation — run as the normal user.
- **Gotcha — `409 ALREADY_EXISTS`.** The app already existed (created by the **dev** target) but **prod**
  has its own state and tried to CREATE it. Fix = adopt, don't rename:
  `bundle deployment bind customer360 customer360 --target prod --auto-approve`. Consequence: dev + prod
  now manage the **same** physical app — `deploy --target dev` reverts it to `source_code_path`,
  `--target prod` restores git-source. Post-T8, prod is the submission source of truth.
- **Gotcha — Git Proxy cluster.** `bundle deploy` (config) succeeds, but `bundle run` (the real GitHub
  pull) routes through the workspace **Git Proxy** cluster (`enableGitProxy: true`,
  `gitProxyClusterId: 0702-202607-z9tgxva7`) and failed `NOT_FOUND: … Cluster is not running` — the
  shared proxy cluster had auto-terminated (120-min idle). Fix = `databricks clusters start
  0702-202607-z9tgxva7`, wait for RUNNING, re-run. *Corrected understanding (thanks to a live
  counter-example — a Git **folder** on this same workspace syncs public github fine): this is NOT an
  egress/private-repo issue. Git **folders** clone via the control plane directly; git-source **Apps**
  route their pull through the Git Proxy cluster when `enableGitProxy` is on — a different code path,
  independent of repo visibility.* No proxy to stand up — just keep the designated cluster running (or,
  on a workspace you admin, repoint `gitProxyClusterId` at a cluster you own).
- **`resources/lakebase.yml` deliberately skipped** (not required for done-when): the 3 synced tables are
  live/online from T1; letting DABs manage them risks a recreate that disrupts active sync. Kept as a
  documented future item.
- **Verified (done-when all met):** app `RUNNING`; active deployment shows **git-source** (repo + branch
  `main` + path `app`), **not** a folder upload; `resolved_commit b3e2bd28…` **matches local `main` HEAD**;
  UI Source page shows the git repository + branch. Submission deploy path is now
  `bundle deploy/run --target prod`.

### ✅ T3a — External API: partner access via M2M
A **second, separate auth boundary** on top of the in-app APIs: partner systems pull customer data
**without the app UI**, authenticating as a **service principal** via **machine-to-machine (M2M)** OAuth.
- **The realization that makes this small:** the Apps proxy treats a *machine* caller exactly like a
  *human* caller — both authenticate to the proxy, which strips `Authorization` and forwards
  `X-Forwarded-Access-Token`. So the handler is nearly identical to the in-app metrics path; the
  difference is entirely in **who mints the token and what grants they hold**, not in request handling.
- **Endpoint:** `GET /api/external/customers/{id}` (`app/backend/routers/external.py`, prefix
  `/api/external` so it's visibly separate). Returns the **same `CustomerDetail` shape** as the in-app
  detail endpoint, but reads **Delta gold via the SQL warehouse** using the caller's bearer (OBO) — it
  **never touches Lakebase and never falls back to the app SP**. Reuses `obo_client()` (401s with no
  token, no SP fallback) + `warehouse.run_query()` (parameterized, PermissionDenied→403, failure→502).
- **Dedicated least-privilege partner SP** (`cust360-partner`, not the app's own SP — the *realistic*
  test). Created via `service-principals create`; OAuth secret via `service-principal-secrets-proxy
  create`. Holds **only** what the endpoint needs: `CAN_USE` on the app (the Apps-proxy gate), `CAN_USE`
  on the warehouse, and `USE CATALOG`/`USE SCHEMA` + `SELECT` on `gold.customers`/`gold.transactions`
  (nothing else — no notes/overrides/audit, no Lakebase). client_id `ec2a70c0-…`, numeric id
  `146608447413191`.
- **Two independent permission gates** (worth showing in the writeup): a valid OAuth bearer is necessary
  but not sufficient. Missing **CAN_USE on the app** → the *proxy* returns **401** before your code runs;
  missing **warehouse/UC SELECT** → your code runs but the *warehouse* returns **INSUFFICIENT_PERMISSIONS**
  (wrapped as 403). A fresh SP with zero starting access makes each gate individually observable.
- **M2M flow:** the SP does **not** send its `client_secret` as the Bearer. The SDK runs the OAuth
  `client_credentials` grant against `/oidc/v1/token` and returns a short-lived `access_token` — *that's*
  the Bearer. Helper `examples/_token.py::m2m_bearer()` wraps this (`oauth_service_principal(cfg)` returns
  an `{"Authorization": "Bearer …"}` header; we strip the scheme). Tests: `examples/m2m_test.py` (happy
  path) + `examples/README.md`. `requests` is a dev/test dep only — intentionally **not** in
  `app/requirements.txt`.
- **Gotcha — `deploy` alone doesn't restart the app.** After `bundle deploy --target dev` uploaded the
  new code, the first test returned **`200` with the SPA `index.html`**, not JSON — the *running* app was
  still the old T8 git-source build (no external router), so `/api/external/…` missed all routes and the
  catch-all served the frontend. Fix = `bundle run customer360 --target dev` to start a **new deployment**
  from the uploaded source. (Deploy = upload + config; run = new serving deployment.)
- **CLI version matters.** The first `deploy` failed with `Invalid update mask … resources[3].job.id`
  under CLI **v0.291.0** (`~/.local/bin/databricks`, first on PATH). Re-running with **v1.5.0**
  (`/opt/homebrew/bin/databricks`) succeeded — now used for all bundle commands to match the workspace CLI.
- **Verified end-to-end (both done-when met):**
  - **#1 — `m2m_test.py` → `200` + customer JSON** (partner SP bearer, minted via client_credentials):

    ```text
    Minting M2M bearer for client_id ec2a70c0-4ef1-443a-b2ca-1937cc8fa205 ...
      got OAuth access_token (client_credentials grant).
    GET .../api/external/customers/C0000000
    -> 200
    {
      "profile": {
        "customer_id": "C0000000", "first_name": "James", "last_name": "Chen",
        "email": "james.chen0@example.com", "country": "US", "city": "New York",
        "segment_id": "S8", "lifetime_value": 66750.76, "churn_score": 0.687, ...
      },
      "recent_transactions": [ { "transaction_id": "TS0000000", "transaction_date": "2026-07-13",
        "channel": "web", "status": "completed", "amount": 1376.04 }, ... 11 completed txns ... ]
    }
    OK: 200 + customer JSON
    ```

    (Run with `DATABRICKS_CLIENT_SECRET=<redacted>` — the partner SP's OAuth secret is never committed.)
  - **#2 — audit attribution.** `system.query.history` shows **both** SELECTs (customers + transactions)
    attributed to `executed_by ec2a70c0-…` / `executed_by_user_id 146608447413191` = **`cust360-partner`**,
    *not* the deploying user and *not* the app SP. Surfaced after ~2 min ingestion lag. This is the clean
    proof of the boundary: a distinct partner identity ran the read, exactly as OBO (no-SP-fallback) intends.
- **Reflection points:** M2M vs. human OBO is the **same proxy path + same handler code** (uniform Apps
  auth model); a dedicated least-privilege partner SP is production-shaped (not the app impersonating
  itself); the two-gate failure modes demonstrate the boundary explicitly.
- **Deployed on dev; not yet promoted to prod.** Prod is git-source (serves committed code only), so T3a
  reaches the submission app after commit/push + `deploy`/`run --target prod` (Git Proxy cluster running).

### ✅ T9 — Lakebase ops: branching, PITR, query insights
Pure **database operations** (no app code) run from one reproducible, self-cleaning script
(`lakebase/ops/t9_branch_pitr_queryperf.py`); captured output in `lakebase/ops/t9_run_output.txt`.
Because we're **not submitting screenshots**, the *recorded numbers* below are the artifact.

- **Model note (important).** Our instance `ai27-lb-apps-capstone` is the **flat *Database Instance***
  model, not the newer *Postgres Projects* model the T9 doc links point at. So a **"branch" = a child
  instance** created via `parent_instance_ref`, and **PITR = a child created at
  `parent_instance_ref.branch_time`** (a past UTC timestamp / WAL LSN within the retention window; ours
  is **7 days**). Same skills graded, slightly different verbs than the linked docs.
- **Two platform constraints discovered live** (both shaped the design):
  1. **Nested children are disabled** — you *cannot* create a child from a child. So a PITR restore must
     root at a **top-level** instance, not off a branch.
  2. **`force` delete is unsupported** — delete children *before* their parent; no force flag.
- **How the two skills were split onto the right substrate:**
  - **T9a — branching + isolation → against REAL PROD.** Branch prod → `…-branch` (copy-on-write, comes
    up with the exact 7 rows), run the destructive `DELETE FROM customer_notes_staging` **on the branch**,
    and show the **parent is untouched**. The branch is the blast radius; prod keeps serving.
  - **T9b — genuine PITR recovery + query perf → on a DEDICATED THROWAWAY instance** (`ai27-lb-t9-demo`).
    Deleting on prod is never acceptable, and PITR can't nest under a branch, so recovery of *actually
    deleted* data is demonstrated on a top-level throwaway lineage: insert N=500 → capture T0 → `DELETE`
    (rows really gone) → **PITR child @ T0 brings all 500 back**.
- **Safety:** every destructive statement is guarded by `assert host != PARENT_HOST` so it can never run
  against prod; connect as the **current user** with a **per-instance minted** DB credential
  (`generate_database_credential(instance_names=[…])`); children created at **CU_1**; **teardown deletes
  everything the script created** (verified: `list-database-instances` shows no T9 leftovers, prod intact).
- **Verified results (from `t9_run_output.txt`):**

  ```text
  T9a  branching + isolation (real prod):
       branch B1 = 7 (==N=7); DELETE→0; parent stayed 7 → ISOLATION PROVEN
  T9b  PITR recovery (throwaway instance):
       inserted N=500; DELETE→0; PITR@T0→500 → RECOVERED
  T9b  query insights (server-side p95, network-independent):
       seeded 200000 rows; target 'user42@acme.example' matches 400
       before plan=Seq Scan          p95= 16.360 ms (client p95 248 ms incl. RTT)
       after  plan=Bitmap Heap Scan   p95=  0.891 ms (client p95 300 ms incl. RTT)
       speedup ≈ 18.4× (server-side p95)
  ```

- **Measurement gotcha (worth calling out).** A naïve client-side wall-clock p95 showed a *misleading*
  1.3× "improvement" — because the ~300 ms **laptop→Azure round-trip** dominates and is identical
  before/after, swamping the query gain. The honest metric is **server-side execution time** via
  `EXPLAIN (ANALYZE, FORMAT JSON)` (`"Execution Time"`), which is network-independent: **16.4 ms → 0.89 ms
  ≈ 18×**. This matches `pg_stat_statements` (`mean_exec_time` **13.7 ms → 1.08 ms**) — two independent
  server-side sources agree. The `WHERE actor_email = …` point lookup goes **Seq Scan → index/bitmap scan**
  once `CREATE INDEX … (actor_email)` exists.
- **Reflection points:** branching is "git for the database" — instant copy-on-write clones let you nuke a
  copy of prod to test a destructive change while prod serves; **PITR does not roll a parent back in place**
  — it *produces a new instance* holding the historical state, so "restore" = "branch at a past timestamp,
  then read/copy the recovered rows"; and the missing-index story is only visible **server-side** once the
  table is large enough (a point query on prod's ~16 real audit rows would show nothing — hence seeding
  200k rows on the throwaway).
