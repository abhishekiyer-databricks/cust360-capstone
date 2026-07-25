# T1 + T8-min Plan — Reverse ETL Tables + Minimal Git-Source Deploy

> **Goal:** Wire the Lakebase read path (synced tables mirrored from gold) and the write
> path (app-owned staging tables), **and** stand up a minimal git-source DABs app so the
> app's service principal exists — which is what unblocks the SP grants. This is the
> **foundation** — no app feature works until this exists.
>
> **Why T1 & T8-min are ONE plan:** the SP grant step (T1-b) needs the app's service
> principal, and that SP is only created on the **first deploy**. So the sequence is
> genuinely interleaved: **T1-a (create tables) → T8-min (deploy hello-world, get SP) →
> T1-b (grant SP)**. Splitting them into two plans would just mean cross-referencing back
> and forth. The *full* T8 (resources/*.yml, git creds bound to SP, committed React bundle,
> declarative lakebase.yml) stays a separate late-stage plan — we only do the minimal
> deployable skeleton here.
>
> **See also:** `master_plan.md` §2 (architecture), §3-D1 (sync modes), §3-D2 (auth),
> §3-D5 (deploy model), §5 (task order steps 1-3).

## Sequence at a glance
1. **T1-a** — create 3 synced tables + 3 staging tables (§3 steps 1-5). No SP yet → no grants.
2. **T8-min** — minimal git-source DABs app (hello-world FastAPI), `bundle deploy` + `run`;
   capture the app **service_principal_id** / **client_id** (§8).
3. **T1-b** — run the SP grant script now that the SP role exists in Lakebase (§2-D5, §3 step 6).

---

## 1. Concept — what T1 teaches and why

Your app needs two things Lakebase provides that Delta gold cannot:

1. **Sub-10ms customer reads.** Delta is analytical (OLAP) — great for scans/aggregates,
   slow for "fetch customer C0003600 now." Lakebase is Postgres (OLTP) — indexed point
   lookups in single-digit ms. **Synced tables** are Databricks-managed Postgres copies of
   gold tables, auto-refreshed. This is **reverse ETL**: pushing curated analytical data
   *back* into an operational store so an app can serve it.

2. **A place to write** notes / segment overrides *without touching gold*. Synced tables are
   read-only mirrors (Databricks owns them; a sync would clobber app writes). Gold is curated
   and slow for row-by-row OLTP writes. So we create **staging tables** — plain, app-owned,
   writable Postgres tables. The app INSERTs here; a later forward-ETL job (T7) merges them
   into gold.

**Two sets of objects, two directions:**
- Synced (read): `customers_synced`, `transactions_synced`, `products_synced` ← gold
- Staging (write): `customer_notes_staging`, `customer_segment_overrides_staging`, `customer_audit_log`

---

## 2. Design decisions

### D1 — Sync mode per table (graded — justify in reflection)
| Synced table | Source (gold) | Mode | Why |
|---|---|---|---|
| `customers_synced` | `ai_27.lakebase_apps_capstone_gold.customers` | **CONTINUOUS** | LTV/churn/segment changes must show in-app within seconds. |
| `transactions_synced` | `….transactions` | **CONTINUOUS** | Recent-activity feed; freshness matters. |
| `products_synced` | `….products` | **TRIGGERED (hourly)** | 200-row slow-changing catalog; continuous streaming would waste compute for near-zero benefit. |

> Reflection line to keep: "products is TRIGGERED hourly because the catalog is slow-changing
> (200 rows, rare edits); customers/transactions are CONTINUOUS because the app must reflect
> upstream LTV/churn/activity changes live."

### D2 — Where synced tables land
Synced tables are created **in the Lakebase-backed UC catalog**: `ai27_lb_apps_capstone.public.*`.
They physically live in the Postgres instance `ai27-lb-apps-capstone`, exposed through UC.
Source tables stay the source of truth in `ai_27.lakebase_apps_capstone_gold`.

### D3 — Pipeline storage
Synced tables run on a managed pipeline that needs a UC storage location
(`storage_catalog.storage_schema`). We'll use **`ai_27.pipelines`** (catalog we own; the
notebook's default `capstone.pipelines` doesn't exist here). The setup script `CREATE SCHEMA
IF NOT EXISTS`-es it.

### D4 — Staging schema (final DDL — matches reference notebook 03)
```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- for gen_random_uuid()

CREATE TABLE IF NOT EXISTS customer_notes_staging (
    note_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id  VARCHAR(20)  NOT NULL,
    author_email VARCHAR(200) NOT NULL,
    note_text    TEXT         NOT NULL,
    sentiment    REAL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    processed    BOOLEAN      NOT NULL DEFAULT FALSE,   -- forward-ETL hook (T7)
    processed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_notes_customer     ON customer_notes_staging (customer_id);
CREATE INDEX IF NOT EXISTS idx_notes_unprocessed  ON customer_notes_staging (processed) WHERE processed = FALSE;

CREATE TABLE IF NOT EXISTS customer_segment_overrides_staging (
    override_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      VARCHAR(20)  NOT NULL UNIQUE,      -- UNIQUE → override is idempotent (UPSERT, T3)
    override_segment VARCHAR(10)  NOT NULL,
    reason           TEXT,
    author_email     VARCHAR(200) NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    processed        BOOLEAN      NOT NULL DEFAULT FALSE,
    processed_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS customer_audit_log (
    audit_id    BIGSERIAL    PRIMARY KEY,               -- sequence → needs USAGE grant for SP
    customer_id VARCHAR(20)  NOT NULL,
    action      VARCHAR(50)  NOT NULL,
    actor_email VARCHAR(200) NOT NULL,
    payload     JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_customer ON customer_audit_log (customer_id);
```
Key design notes: `customer_id UNIQUE` on overrides enables idempotent UPSERT (T3
done-when); `processed`/`processed_at` are the forward-ETL hooks (T7); `BIGSERIAL` on audit
implies a sequence that the SP will need `USAGE` on (see grants).

### D5 — The SP grants (THE "best practices" the task calls out)
Fresh Postgres roles have **zero privileges**. The app's service principal connects to
Lakebase as a PG role whose **name is the SP's `client_id` UUID**. Until we grant it, every
app query fails with "permission denied." One-time grant step:
```sql
-- <SP_ROLE> = app service principal client_id (a UUID)
GRANT USAGE ON SCHEMA public TO "<SP_ROLE>";

-- read synced tables
GRANT SELECT ON customers_synced, transactions_synced, products_synced TO "<SP_ROLE>";

-- read/write staging
GRANT SELECT, INSERT, UPDATE ON customer_notes_staging,
    customer_segment_overrides_staging, customer_audit_log TO "<SP_ROLE>";

-- sequences (BIGSERIAL audit_id, any others)
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "<SP_ROLE>";

-- future synced tables inherit SELECT automatically
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "<SP_ROLE>";
```

### ⚠️ D6 — Ordering catch (important)
The grant step requires that **the app SP exists AND has logged into Lakebase at least once**
(the PG role is created lazily on first login). But the app SP is only created when the app is
**first deployed** (master plan step 2, "T8-min"). So T1 splits into two phases:

- **T1-a (now):** create synced tables + staging tables. (No app SP yet → skip grants.)
- **T1-b (right after first deploy, step 2):** once the app SP exists and has hit Lakebase
  once, run the grant script. If we try grants before the role exists, `GRANT ... TO "<uuid>"`
  fails with "role does not exist."

This is expected and called out here so it isn't a surprise. We'll revisit T1-b immediately
after the minimal deploy.

### D7 — How we author it (committable + re-runnable)
Everything lives under `lakebase/reverse_etl/` so it's part of the repo (T8 wants no drift):
```
lakebase/reverse_etl/
├── 01_create_synced_tables.py   # SDK: create_synced_database_table (idempotent get-or-create)
├── 02_create_staging_tables.py  # psycopg: DDL above
└── 03_grant_sp.py               # psycopg: the D5 grants (run in T1-b, param: sp_client_id)
```
These run **as notebooks/jobs against the workspace** (they need the workspace network + a
Lakebase OAuth token), submitted via the CLI like the installer did — not on the Mac.
Later (T8) the synced-table specs also get a **declarative `resources/lakebase.yml`** so the
bundle owns them; the scripts remain for the imperative/first-run path and reflection.

---

## 3. Step-by-step implementation

**Step 1 — Author `01_create_synced_tables.py`.** Uses `WorkspaceClient.database
.create_synced_database_table` with `SyncedTableSpec(source_table_full_name, primary_key_columns,
scheduling_policy, new_pipeline_spec=NewPipelineSpec(storage_catalog, storage_schema))`.
Get-or-create pattern (catch "already exists"). Three calls: customers/transactions
(CONTINUOUS), products (TRIGGERED). PKs: `customer_id`, `transaction_id`, `product_id`.

**Step 2 — Author `02_create_staging_tables.py`.** Connect via psycopg to `PGHOST:5432`
db `cust360ai27`, `user = current_user()`, `password = w.config.oauth_token().access_token`,
`sslmode=require`, `autocommit=True`. Execute the D4 DDL. Print `\dt`-style listing.

**Step 3 — Author `03_grant_sp.py`.** Same connection; takes the app SP `client_id` as a
widget/param; runs the D5 grants. **Do not run yet** (T1-b, after first deploy).

**Step 4 — Run Steps 1 & 2 against the workspace.** Submit as one-shot jobs via the CLI
(mirror the installer's `jobs submit` flow), params from `app/.env`:
`catalog=ai_27`, `schema=lakebase_apps_capstone_gold`, `uc_lakebase_catalog=ai27_lb_apps_capstone`,
`instance_name=ai27-lb-apps-capstone`, `database_name=cust360ai27`,
`storage_catalog=ai_27`, `storage_schema=pipelines`.

**Step 5 — Verify** (see §4).

**Step 6 — (T1-b, deferred)** After minimal deploy: fetch app SP `client_id` from
`databricks apps get customer360`, then run `03_grant_sp.py`.

---

## 4. How to deploy & test

T1 has **no app to deploy yet** — verification is direct against Lakebase + the UI:

- **Synced tables (CONTINUOUS state):**
  - Lakebase UI → instance `ai27-lb-apps-capstone` → the 3 synced tables show state
    CONTINUOUS (customers, transactions) / TRIGGERED (products), initial sync complete.
  - Or CLI: `databricks database get-synced-database-table
    ai27_lb_apps_capstone.public.customers_synced --profile DEFAULT -o json` → check
    `data_synchronization_status`.
- **Staging tables exist with right columns:** psql/psycopg `\dt` shows the 3 tables;
  `\d customer_notes_staging` shows `processed BOOLEAN DEFAULT false`, etc.
- **Row sanity:** `SELECT count(*) FROM customers_synced;` ≈ 10,000 once synced.
- **(T1-b) grants:** after grant, connecting as the SP role and running `SELECT 1 FROM
  customers_synced LIMIT 1` and an INSERT into a staging table both succeed.

---

## 5. Done-when checklist (from the task doc)

- [ ] All 3 synced tables show **CONTINUOUS/TRIGGERED** state in the Lakebase UI
      (customers + transactions CONTINUOUS; products TRIGGERED)
- [ ] All 3 staging tables exist (`\dt`) with the right columns (`processed BOOLEAN DEFAULT false` present)
- [ ] `customers_synced` row count ≈ 10,000 after initial sync
- [ ] (T1-b, after first deploy) SP grants applied; SP can SELECT synced + SELECT/INSERT/UPDATE staging
- [ ] Reflection note written: sync-mode rationale per table

---

## 6. Risks / gotchas specific to T1
- **Role-does-not-exist:** running grants before the app SP logs into Lakebase once → fails.
  That's why grants are T1-b (post-deploy). (§2-D6)
- **Storage catalog:** must be a catalog we can write to (`ai_27`), not the notebook default `capstone`.
- **Token expiry:** the setup script mints a fresh Lakebase OAuth token at run; if a run is
  slow, tokens last ~1h — fine for one-shot DDL.
- **Continuous sync lag:** initial hydration of `customers_synced`/`transactions_synced` takes
  a few minutes; don't judge "empty" until initial sync completes.
- **Idempotency:** all scripts use `IF NOT EXISTS` / get-or-create so re-runs are safe.

---

## 7. T8-min — minimal git-source deploy (the SP-unblocker)

**Purpose:** get a deployable git-source app so the **app service principal exists** (T1-b
needs it). Keep it minimal — full T8 (all resource bindings, forward-ETL job, committed React
bundle, `lakebase.yml`) comes later.

### 7.1 What we build
- **`app/backend/main.py`** — a tiny FastAPI app with `GET /` (health/hello) and
  `GET /api/health` returning `{"status":"ok"}`. No Lakebase/warehouse yet.
- **`app/app.yaml`** — minimal: `command: ["uvicorn","backend.main:app","--host","0.0.0.0","--port","8000"]`.
  (Scopes/env come in T2/T6.)
- **`databricks.yml`** — bundle root: `bundle.name`, `targets: dev/prod`, workspace host,
  and `variables` we'll grow later.
- **`resources/app.yml`** — the app declared as a **git-source app**:
  `git_repository.provider: github` + `git_repository.url` (our repo), `git_source.branch: main`,
  `git_source.source_code_path: app`. **Do NOT set app-level `source_code_path`** (DABs rejects
  "both git_source and source_code_path set").
- **Scaffold fixes before first deploy (master plan §4):** remove root `app/package.json`
  (Apps runtime would run `npm build` & fail); for the minimal app there's no React bundle yet,
  so static serving is deferred.

### 7.2 The git-credential chicken-and-egg (git-source specific)
A git-source app pulls source **as the app SP**, so the SP needs a GitHub credential — but the
SP doesn't exist until first deploy. Order:
1. `databricks bundle validate --target dev --profile DEFAULT`
2. `databricks bundle deploy --target dev --profile DEFAULT` → creates the app + its SP.
3. `databricks apps get customer360 --profile DEFAULT -o json` → capture
   **`service_principal_id`** (numeric, for git creds) and **`service_principal_client_id`**
   (UUID, = the Lakebase PG role name for T1-b).
4. Register GitHub cred bound to the SP (needs a **GitHub PAT** — open prereq):
   ```
   databricks git-credentials create --json '{
     "git_provider":"gitHub","git_email":"<bot-email>",
     "personal_access_token":"<PAT>","principal_id":<SP_ID>,
     "name":"GitHub credentials for app SP"}' --profile DEFAULT
   ```
5. `databricks bundle run customer360 --target dev --profile DEFAULT` → pulls latest commit,
   starts the app. **`bundle run` is the source-pull+restart, not a job trigger** — run it
   after every deploy.

> Repo must be pushed to GitHub first (this chat sets that up) so `git_repository.url` resolves
> and the SP can pull. Public repo still needs the SP git credential for `bundle run` pulls.

### 7.3 T8-min done-when
- [ ] `bundle validate --target dev` passes
- [ ] App deploys; `databricks apps get customer360` shows it running
- [ ] App SP captured: `service_principal_id` (numeric) + `client_id` (UUID)
- [ ] `bundle run` shows source = **git repo + branch** (not workspace upload)
- [ ] App URL serves the hello/health response

### 7.4 T1-b — run the grants (loop back)
With the SP `client_id` UUID from 7.1-step-3, run `lakebase/reverse_etl/03_grant_sp.py`
(§2-D5). Precondition: the SP must have **logged into Lakebase once** — the first app request
that touches Lakebase does this; until then the PG role may not exist. If the app doesn't hit
Lakebase yet (minimal build doesn't), we trigger one SP Lakebase login explicitly (a `SELECT 1`
as the SP via `generate_database_credential`) before granting. Verify per §4 (T1-b).

---

## 8. Next after T1 + T8-min
→ **T2** auth (`auth.py` obo/sp clients, `db.py` `lakebase_sp()` pool) — first thing: confirm
the **OBO preview toggle** is ON (master plan §10). Then **T3** read path (first real feature
slice). See `master_plan.md` §5 for the full order.
```
