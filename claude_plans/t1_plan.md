# T1 Plan — Reverse ETL: synced + staging tables (+ SP grants)

> **Status: ✅ DONE.** This is the record of T1. It wires the Lakebase **read path**
> (synced tables mirrored from Delta gold) and the **write path** (app-owned staging
> tables), then grants the app service principal the Postgres privileges it needs. This
> is the **foundation** — no app feature works until this exists.
>
> **Note on scope:** T8 (the git-source deploy / DABs production packaging) was **scrapped
> for now** — we deploy via `source_code_path` instead (master_plan §3-D5). The one piece
> of T8 that T1 genuinely depended on was the **first app deploy**, because the app SP (and
> therefore the Postgres role we grant in T1-b) only exists after the app is deployed once.
> That minimal deploy has already happened; the SP details captured from it are recorded in
> §2-D6 and the project memory. Everything about the git-source pattern lives in a future
> T8 plan, not here.
>
> **See also:** `master_plan.md` §2 (architecture), §3-D1 (sync modes), §3-D5 (deploy model).

## Sequence at a glance
1. **T1-a** — create 3 synced tables + 3 staging tables (§3 steps 1-5). No SP yet → no grants.
2. *(app deployed once → app SP now exists; see §2-D6)*
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
(`storage_catalog.storage_schema`). We use **`ai_27.pipelines`** (catalog we own; the
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

### D6 — Ordering catch: grants need the app SP (which the first deploy created)
The grant step requires that **the app SP exists AND has a Postgres role** (the PG role is
created lazily on first Lakebase login). The app SP is only created when the app is **first
deployed**. So T1 split into two phases:

- **T1-a:** create synced tables + staging tables. (No app SP yet → skip grants.)
- **T1-b:** once the app SP existed, run the grant script.

**Actuals captured from the first deploy** (app `customer360`, `source_code_path` deploy):
- app SP **`client_id = 10c64b22-ac46-4123-bb60-041dc9d4fa92`** (= the Lakebase PG role name)
- app SP numeric id `144899163311454`

Because the minimal app doesn't hit Lakebase on its own, the PG role was **pre-created**
before granting (see §6 gotcha #2), rather than relying on a lazy first-login.

### D7 — How we authored it (committable + re-runnable)
Everything lives under `lakebase/reverse_etl/` so it's part of the repo:
```
lakebase/reverse_etl/
├── 01_create_synced_tables.py   # SDK: create_synced_database_table (idempotent get-or-create)
├── 02_create_staging_tables.py  # psycopg: DDL above
└── 03_grant_sp.py               # psycopg: the D5 grants (+ pre-create SP role); param: sp_client_id
```
These run **as notebooks/jobs against the workspace** (they need the workspace network + a
Lakebase OAuth token), submitted via the CLI like the installer did — not on the Mac.

---

## 3. Step-by-step implementation

**Step 1 — `01_create_synced_tables.py`.** Uses `WorkspaceClient.database
.create_synced_database_table` with `SyncedTableSpec(source_table_full_name, primary_key_columns,
scheduling_policy, new_pipeline_spec=NewPipelineSpec(storage_catalog, storage_schema))`.
Get-or-create pattern (catch "already exists"). Three calls: customers/transactions
(CONTINUOUS), products (TRIGGERED). PKs: `customer_id`, `transaction_id`, `product_id`.

**Step 2 — `02_create_staging_tables.py`.** Connect via psycopg to `PGHOST:5432`
db `cust360ai27`, `user = current_user()`, `password = w.database.generate_database_credential(...)`,
`sslmode=require`, `autocommit=True`. Execute the D4 DDL. Print `\dt`-style listing.

**Step 3 — `03_grant_sp.py`.** Same connection; takes the app SP `client_id` as a
widget/param; pre-creates the SP PG role (§6 gotcha #2) then runs the D5 grants.

**Step 4 — Run Steps 1 & 2 against the workspace.** Submit as one-shot jobs via the CLI
(mirror the installer's `jobs submit` flow), params from `app/.env`:
`catalog=ai_27`, `schema=lakebase_apps_capstone_gold`, `uc_lakebase_catalog=ai27_lb_apps_capstone`,
`instance_name=ai27-lb-apps-capstone`, `database_name=cust360ai27`,
`storage_catalog=ai_27`, `storage_schema=pipelines`.

**Step 5 — Verify** (see §4).

**Step 6 — T1-b.** With the app SP `client_id` (§2-D6), run `03_grant_sp.py`.

---

## 4. How to test

T1 has **no app feature to exercise yet** — verification is direct against Lakebase + the UI:

- **Synced tables state:**
  - Lakebase UI → instance `ai27-lb-apps-capstone` → the 3 synced tables show
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

## 5. Done-when checklist (from the task doc) — ✅ all met

- [x] All 3 synced tables show **CONTINUOUS/TRIGGERED** state
      (customers + transactions CONTINUOUS; products TRIGGERED)
- [x] All 3 staging tables exist (`\dt`) with the right columns (`processed BOOLEAN DEFAULT false` present)
- [x] `customers_synced` row count ≈ 10,000 after initial sync
- [x] SP grants applied; SP can SELECT synced + SELECT/INSERT/UPDATE staging
- [x] Reflection note written: sync-mode rationale per table (§2-D1)

---

## 6. Risks / gotchas hit during T1
1. **Reference notebook uses `w.config.oauth_token()`** which FAILS under job-runtime auth
   ("OAuth tokens not available"). **Fix:** mint the Lakebase token via
   `w.database.generate_database_credential(request_id=uuid, instance_names=[INSTANCE]).token`.
   Used in all reverse_etl scripts.
2. **SP PG role doesn't exist until first Lakebase login.** Since the minimal app doesn't hit
   Lakebase, pre-create it: `CREATE EXTENSION IF NOT EXISTS databricks_auth;
   SELECT databricks_create_role('<sp_client_id>','SERVICE_PRINCIPAL');` — baked into
   `03_grant_sp.py`.
3. **Storage catalog** must be one we can write to (`ai_27`), not the notebook default `capstone`.
4. **Continuous sync lag:** initial hydration of the CONTINUOUS tables takes a few minutes;
   don't judge "empty" until initial sync completes.
5. **Idempotency:** all scripts use `IF NOT EXISTS` / get-or-create so re-runs are safe.
6. **Destroy caveat:** a `bundle destroy` + redeploy mints a **new app SP with a new
   `client_id`**, so the T1-b grants (bound to the old role) no longer apply — re-run
   `03_grant_sp.py` with the new `client_id`. Plain `bundle deploy`/`run` keeps the same SP.

---

## 7. Next after T1
→ **T2** auth (`auth.py` obo/sp clients, `db.py` `lakebase_sp()` pool). See `t2_plan.md`.
First step: confirm the OBO scope setting is ON (already verified — see `process_doc.md`).
