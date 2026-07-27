# T7 Plan — Forward ETL: Lakebase staging → Delta gold (closing the loop)

> **Goal:** materialise the notes / segment-overrides the app writes into Lakebase *staging*
> (T3-3C) back into **Delta gold**, on demand, from a **Reports** page. After T7 a rep clicks
> "Run forward-ETL", a Databricks **job** runs (triggered by the **app SP** via the Jobs API),
> reads unprocessed staging rows over psycopg, **`MERGE INTO`** gold (Spark), marks the rows
> `processed=true`, and the Reports page shows live run status + a recent-runs history. This
> closes the core data story: reverse ETL (T1) → app CRUD (T3) → **forward ETL (T7)**.
>
> **Pattern:** **A — psycopg + `MERGE INTO` Delta (pull / on-demand)** — locked in master_plan
> §3-D3. Pattern B (Lakehouse Sync CDC) is Beta and hides the mechanics we want to demonstrate.
>
> | What flows where (master_plan §3-D3) | |
> |---|---|
> | `customer_notes_staging` | → **NEW** `gold.customer_notes` table (INSERT via MERGE on `note_id`) |
> | `customer_segment_overrides_staging` | → **UPDATE** `gold.customers.segment_id` (MERGE on `customer_id`) |
> | `customer_audit_log` | stays in Lakebase (never promoted) |
>
> **See also:** `master_plan.md` §2 (the two-ETL-direction diagram), §3-D2 (identities: Lakebase
> = SP; the app triggers the job as the SP), §3-D3 (Pattern A + destinations), §5 row 7 (task
> order — T7 before T4), §7 (best practices); `t1_plan.md` (staging DDL — the columns we read);
> `t3_plan.md` §D3 (how writes land in staging + audit). Task doc: `CAPSTONE_TASKS.md` T7.

---

## Pattern B (Lakebase CDF) — evaluated, deliberately NOT chosen

Before building, we re-checked the status of the "native" alternative. The task doc calls it
**Lakehouse Sync (Beta)**; that label is **stale**. As of **2026** (docs verified live) the
feature was renamed **Lakebase Change Data Feed (CDF)** — powered by the `wal2delta` Postgres
extension — and is now **Public Preview** (not Beta, but also **not GA**). So we asked the fair
question: since it showcases a native product feature, is it the better route? We concluded
**no**, for four concrete reasons — this is a reflection talking point, not a limitation we hit:

1. **Still a preview feature.** Public Preview carries the same "not production-blessed" caveat
   as Beta for a capstone whose reflection grades deliberate architecture choices.
2. **The "Run forward-ETL" button maps poorly to it.** CDF replication runs **continuously**
   (batched/flushed from the WAL every ~15s); there is **no on-demand trigger, pause, or flush
   API** — only start/disable at the schema level. So the Reports button could *not* drive the
   staging→gold promotion; it could only kick off a *secondary* dedup-into-gold consumer job.
   That's a muddier demo story than Pattern A, where the button genuinely drives the whole
   promotion end-to-end.
3. **It doesn't remove the job.** CDF only replicates staging → `lb_*_history` (SCD2 Delta). We
   would **still** need a Spark consumer job to dedup that history into clean gold — so we'd add
   a preview dependency and keep most of the complexity.
4. **It hides the mechanics we want to demonstrate** (master_plan §3-D3). Pattern A makes the
   read→MERGE→mark loop and idempotency (`processed=false`) explicit, and forces us to create
   `gold.customer_notes` — a clean demonstration of the write path. Given zero React/FastAPI
   background and the goal of *understanding* the flow, explicit wins.

**When we'd revisit Pattern B:** high-volume / streaming change capture, or when a full SCD2
audit history of every staging mutation is itself the deliverable — and once it reaches GA.

> **Reflection note (carry into `process_doc.md` at task close):** we chose Pattern A (GA
> primitives: Jobs + Spark `MERGE`) over Pattern B (Lakebase CDF, Public Preview) as a
> deliberate trade-off — on-demand button semantics, explicit idempotency, and pedagogical
> clarity — while knowing the native feature exists.

---

## 0. How T7 is sliced (2 deploy checkpoints)

T7 is smaller than T3, but the risky part is the **data plane** (a Spark job doing `MERGE INTO`
gold, cross-system idempotency). We prove that in isolation first, *then* wire the thin app
surface on top — same philosophy as T3's slices, so a failure is small and localized.

| Slice | What it builds | Tested by / "working" looks like |
|---|---|---|
| **7A — the ETL job** | The forward-ETL **notebook** (`lakebase/forward_etl/pattern_a_psycopg2/`), the **`gold.customer_notes`** table (created idempotently by the job), and the **`resources/jobs.yml`** DABs job resource (incl. app-SP run permission) | Insert a test staging note + override → run the job (UI or `bundle run`) → `gold.customer_notes` gains the row, `gold.customers.segment_id` updates, staging rows flip `processed=true`. Re-run with nothing new = no-op. |
| **7B — app wiring + Reports page** | `routers/jobs.py` (`POST /run-forward-etl`, `GET /{run_id}`, `GET /runs`) triggered by the **app SP**; `FORWARD_ETL_JOB_ID` env; `Reports.tsx` (button + live status + recent-runs table) | On the deployed app: click **Run forward-ETL** → status goes RUNNING → SUCCESS; recent-runs table lists it; a note added via the app then appears in gold after a run. |

> Order rationale: 7A has zero dependency on the app and can be run/verified straight from the
> workspace (or `databricks bundle run`), so we lock the MERGE + idempotency semantics before
> any FastAPI/React code. 7B is then a thin trigger/poll surface over a job we already trust.

---

## 1. Concept — what T7 teaches and why

### 1.1 Why a *forward* ETL exists at all

Reverse ETL (T1) copies gold → Lakebase so the app gets sub-10ms reads. But the app **writes**
(notes, segment overrides) land in Lakebase *staging* tables — deliberately **not** in gold,
because gold is the analytical source of truth and shouldn't be mutated by every UI click.
Forward ETL is the controlled, auditable path that promotes those staging rows **back into
gold**, so analytics / ML / dashboards eventually see rep-authored data. This is the "write
path completes the circle" moment of the whole capstone.

```
     app writes (T3-3C)                    forward ETL (T7 — this task)
  ┌──────────────────────┐   psycopg    ┌───────────────────────────────┐   Spark MERGE   ┌───────────┐
  │ customer_notes_staging│──read WHERE──│  Job (Pattern A notebook)     │────INTO────────▶│ gold.     │
  │ ..._overrides_staging │  processed=  │  1. read unprocessed rows     │                 │ customer_ │
  └──────────────────────┘   false      │  2. MERGE INTO gold            │                 │ notes /   │
             ▲                            │  3. UPDATE staging processed=T│                 │ customers │
             └──── mark processed ────────┘   (Postgres txn)             └───────────┘
                                          triggered by APP SP via Jobs API ◀── Reports "Run" button
```

### 1.2 The critical subtlety: there is **no single cross-system transaction**

The task text says "…`MERGE INTO` gold … then `UPDATE *_staging SET processed=true` … in the
same transaction." Read carefully: the MERGE writes **Delta (Spark)** and the flag-update writes
**Postgres (Lakebase)** — **two different systems**. You *cannot* wrap both in one atomic
transaction. So the real guarantee is **idempotency by design**, not distributed atomicity:

1. **Read** the unprocessed rows and **capture their ids** (`note_id`s / `override_id`s).
2. **MERGE** into gold, keyed on the **primary/business key** (`note_id`, `customer_id`) —
   re-merging the same row is a no-op (no duplicates).
3. **`UPDATE` staging** `SET processed=true` **only for the ids captured in step 1**, in a
   Postgres transaction.

If the job dies between step 2 and step 3, the rows are still `processed=false`, so the next run
re-MERGEs them (harmless — idempotent on the key) and re-marks them. **We never lose or
duplicate data.** This is the key teaching point — call it out in the reflection: *"same
transaction" applies to the Postgres side; cross-system integrity comes from MERGE-on-key +
the `processed` flag, not a 2-phase commit.*

### 1.3 Who runs what (identities — master_plan §3-D2)

- **The app triggers the job as the app SP.** `POST /api/jobs/run-forward-etl` uses
  `sp_client()` (the app SP, `10c64b22-…`) to call `jobs.run_now`. So the SP needs
  **CAN_MANAGE_RUN** on the job (granted declaratively in `jobs.yml`).
- **The job itself runs as the deploying user** (`abhishek.iyer`) in dev — see D3. The user
  already owns the gold tables (MERGE works) and has full Lakebase access (owns the staging
  tables), so **7A needs zero new grants**. Production hardening (run the job *as the SP* +
  grant the SP `MODIFY` on gold) is a T8/reflection note, not a T7 blocker.

### 1.4 Why a Spark notebook job (not a warehouse statement)

`MERGE INTO` on a managed Delta table is the canonical Pattern A. Task says "build a Spark
DataFrame, `MERGE INTO`…". We read the small unprocessed set over psycopg, build a Spark
DataFrame, register a temp view, and run `spark.sql("MERGE INTO …")`. Serverless job compute
means no cluster to manage.

---

## 2. Design decisions

### D1 — Pattern A, notebook job, serverless compute
- **Pattern A** (psycopg + `MERGE INTO`), master_plan §3-D3. One notebook,
  `lakebase/forward_etl/pattern_a_psycopg2/forward_etl_merge.py` (Databricks notebook-source
  `.py`, like the reverse_etl scripts).
- **Serverless job compute** (no cluster config). The notebook does
  `%pip install --quiet --upgrade databricks-sdk psycopg2-binary` first (reverse-ETL gotcha #4:
  cluster SDK too old for `.database`; serverless is fine but pin the upgrade for parity).
  > **Action at impl:** if serverless notebook tasks aren't enabled on this workspace, fall
  > back to a single-node job cluster in `jobs.yml`. Resolve on first `bundle deploy`.
- Connect to Lakebase exactly like the reverse_etl scripts: `psycopg2.connect(host=read_write_dns,
  user=current_user, password=generate_database_credential(...).token, sslmode=require)`.

### D2 — Destinations & the `MERGE` statements (master_plan §3-D3)
- **`gold.customer_notes` (NEW table).** The job **creates it idempotently** on first run
  (`CREATE TABLE IF NOT EXISTS ai_27.lakebase_apps_capstone_gold.customer_notes (...)`) so T7 is
  self-contained. Columns mirror the staging shape (see `t1_plan`/02 DDL):
  `note_id STRING, customer_id STRING, author_email STRING, note_text STRING, sentiment FLOAT,
  created_at TIMESTAMP, merged_at TIMESTAMP`.
  ```sql
  MERGE INTO ai_27.lakebase_apps_capstone_gold.customer_notes t
  USING staged_notes s ON t.note_id = s.note_id
  WHEN NOT MATCHED THEN INSERT (note_id, customer_id, author_email, note_text, sentiment,
                                created_at, merged_at)
    VALUES (s.note_id, s.customer_id, s.author_email, s.note_text, s.sentiment,
            s.created_at, current_timestamp());
  ```
  (INSERT-only on `note_id` = append semantics + dedupe on re-run. Notes are immutable, so no
  `WHEN MATCHED`.)
- **`gold.customers.segment_id` (UPDATE).** Overrides change an existing customer's segment:
  ```sql
  MERGE INTO ai_27.lakebase_apps_capstone_gold.customers t
  USING staged_overrides s ON t.customer_id = s.customer_id
  WHEN MATCHED THEN UPDATE SET t.segment_id = s.override_segment, t.updated_at = current_timestamp();
  ```
  (Idempotent: applying the same segment twice is a no-op.)
- **`customer_audit_log` is never promoted** — it stays in Lakebase (master_plan §3-D3).

### D3 — Job `run_as` = deploying user (dev); app SP gets CAN_MANAGE_RUN
- **Dev:** DABs `mode: development` runs the job as the **deployer** (`abhishek.iyer`), who owns
  gold + staging → **no new UC/PG grants needed for 7A**. Simplest correct thing.
- **App-SP trigger:** grant the app SP **CAN_MANAGE_RUN** on the job via a `permissions:` block
  in `jobs.yml` (`service_principal_name: 10c64b22-ac46-4123-bb60-041dc9d4fa92`). Without it,
  `sp_client().jobs.run_now` → `PERMISSION_DENIED`.
- **Production note (T8/reflection, NOT T7):** switch the job to `run_as` the app SP and grant
  the SP `USE CATALOG/SCHEMA` + `MODIFY`/`SELECT` on the gold tables, so the whole forward path
  is SP-owned end-to-end. Document as "next optimization."

### D4 — The job resource lives in `resources/jobs.yml` (bundle-managed from now)
- We already deploy via DABs (`source_code_path`), so define the job **declaratively** rather
  than clicking it together — no drift, and it's the exact artifact T8 formalizes. `jobs.yml`
  is included via the existing `include: [resources/*.yml]` in `databricks.yml`.
- **Notebook source:** `notebook_task.notebook_path` points at the bundle-relative notebook
  (`../lakebase/forward_etl/pattern_a_psycopg2/forward_etl_merge`); `bundle deploy` uploads it
  to the workspace automatically. Pass `instance_name` / `database_name` as job **parameters**
  (like the reverse_etl notebooks' widgets) so nothing is hardcoded.

### D5 — Wiring the job id into the app: plain env now, `valueFrom` at T6
- The app needs the job's numeric id to trigger it. **Two options:**
  - **(a) `valueFrom` binding** — bind the job as an app resource in `resources/app.yml` and
    reference it in `app.yaml` via `valueFrom`. This is the *documented production pattern* and
    what **T6** finalizes (master_plan T6: "`FORWARD_ETL_JOB_ID` comes via `valueFrom`").
  - **(b) plain env** — after the first `bundle deploy` creates the job, read its id
    (`databricks bundle summary` / job UI) and set `FORWARD_ETL_JOB_ID` as a normal value in
    `app.yaml env`.
- **Decision: (b) for T7, upgrade to (a) in T6.** Rationale: consistent with how we've handled
  every other "minimal now, formalize later" call (T2 non-secret env → T6 secrets). It keeps 7B
  unblocked without pulling the app-resource binding forward. Add a `# TODO(T6): valueFrom`
  comment on the env line. `config.py` reads `FORWARD_ETL_JOB_ID`; the router returns **503** if
  it's unset (clear failure, not a 500).

### D6 — App endpoints: trigger + poll + history, all via `sp_client()` (Jobs API)
`routers/jobs.py`, `APIRouter(prefix="/api/jobs")`:
- `POST /run-forward-etl` → `sp_client().jobs.run_now(job_id=FORWARD_ETL_JOB_ID)`; return
  `{run_id, run_page_url, state}`. (No body needed. Optionally accept nothing / ignore body.)
- `GET /{run_id}` → `sp_client().jobs.get_run(run_id)`; return a trimmed
  `{run_id, life_cycle_state, result_state, start_time, end_time, run_page_url}` (Pydantic
  model, not the raw SDK object).
- `GET /runs` → `sp_client().jobs.list_runs(job_id=FORWARD_ETL_JOB_ID, limit=10)`; return the
  recent runs for the history table.
- **SP, not OBO** (master_plan §3-D2: the SP owns job triggering). No `X-Forwarded-*` needed.
- **Timeouts** on the SDK calls (master_plan §7 API hygiene); structured log per trigger.

### D7 — Reports page (task "Reports" UI)
`app/frontend/src/pages/Reports.tsx` replaces the `/reports` placeholder in `App.tsx`:
- **"Run forward-ETL" button** → `useMutation(runForwardEtl)`; on success capture `run_id` and
  start polling.
- **Live status indicator** → `useQuery(['job-run', runId], () => getJobRun(runId),
  { refetchInterval: r => isTerminal(r) ? false : 3000 })` — poll every ~3s until terminal
  (`TERMINATED`/`SKIPPED`/`INTERNAL_ERROR`), then stop. Mantine `Badge` colored by
  `result_state` (SUCCESS=green, FAILED=red, RUNNING=blue with a `Loader`).
- **Recent-runs table** → `useQuery(['job-runs'], listJobRuns, { staleTime: 10s })`, invalidated
  when a new run finishes. Columns: run id (link to `run_page_url`), state, start, duration.
- API client additions in `api/client.ts`: `runForwardEtl()`, `getJobRun(runId)`,
  `listJobRuns()` + the matching TS types.

---

## 3. Step-by-step implementation

### Slice 7A — the ETL job

1. **`lakebase/forward_etl/pattern_a_psycopg2/forward_etl_merge.py`** (Databricks notebook
   source). Structure mirrors the reverse_etl notebooks:
   - `%pip install --quiet --upgrade databricks-sdk psycopg2-binary` → `dbutils.library.restartPython()`.
   - Widgets: `instance_name` (default `ai27-lb-apps-capstone`), `database_name`
     (default `cust360ai27`), `catalog` (`ai_27`), `schema` (`lakebase_apps_capstone_gold`).
   - Connect to Lakebase as `current_user` with a minted `generate_database_credential` token
     (copy the pattern from `02_create_staging_tables.py`).
   - `CREATE TABLE IF NOT EXISTS <catalog>.<schema>.customer_notes (...)` (D2 schema).
   - **Notes:** `SELECT note_id, customer_id, author_email, note_text, sentiment, created_at
     FROM customer_notes_staging WHERE processed = false` → capture `note_id`s → if non-empty:
     `spark.createDataFrame(rows)` → `createOrReplaceTempView("staged_notes")` → `spark.sql(MERGE …)`.
   - **Overrides:** `SELECT override_id, customer_id, override_segment FROM
     customer_segment_overrides_staging WHERE processed = false` → capture `override_id`s → if
     non-empty: temp view `staged_overrides` → `spark.sql(MERGE INTO … customers …)`.
   - **Mark processed** (Postgres txn, AFTER the MERGEs succeed): `UPDATE …_staging SET
     processed=true, processed_at=NOW() WHERE <id> IN (captured ids)` for each table, then
     `conn.commit()`. Use `IN %(ids)s` / `ANY(%s)` parameterized — never string-format ids.
   - **Empty-safe:** if both selects return nothing, skip everything and exit (no-op run).
   - `dbutils.notebook.exit(json.dumps({"notes_merged": n, "overrides_merged": m}))` for
     observability in run output.
2. **`resources/jobs.yml`** (new) — DABs job `forward_etl` (D4):
   - `name: customer360_forward_etl`, single `notebook_task` → the notebook above, with
     `base_parameters` for instance/db/catalog/schema.
   - Serverless compute (D1) — omit `new_cluster`; add a job cluster fallback comment.
   - `permissions:` → `- level: CAN_MANAGE_RUN`, `service_principal_name: <app SP client_id>`
     (D3).
3. **Deploy & run 7A** (§4). Seed a staging row (via the deployed app's Add-note, or a manual
   psycopg INSERT), run the job, verify gold + `processed` flip + idempotent re-run.

### Slice 7B — app wiring + Reports page

4. **`app/backend/config.py`** — add `FORWARD_ETL_JOB_ID = _get("FORWARD_ETL_JOB_ID")`.
5. **`app/app.yaml`** — add `env: FORWARD_ETL_JOB_ID` = the id from 7A's deploy
   (`# TODO(T6): switch to valueFrom binding of the jobs resource`).
6. **`app/backend/models.py`** — add `JobRun` (run_id, life_cycle_state, result_state,
   start_time, end_time, run_page_url) and `JobRunTriggered` (run_id, run_page_url, state).
7. **`app/backend/routers/jobs.py`** (new) — the three endpoints (D6); 503 if
   `FORWARD_ETL_JOB_ID` unset; trim SDK objects to the Pydantic models; timeouts + logging.
8. **`app/backend/main.py`** — `app.include_router(jobs.router)`; bump the `stage` string.
9. **`app/frontend/src/api/client.ts`** — `JobRun`/`JobRunTriggered` types +
   `runForwardEtl()`, `getJobRun(runId)`, `listJobRuns()`.
10. **`app/frontend/src/pages/Reports.tsx`** (new) — button + polling status + recent-runs
    table (D7).
11. **`app/frontend/src/App.tsx`** — replace the `/reports` `Placeholder` with `lazy(() =>
    import("./pages/Reports"))`.
12. **Deploy & test 7B** (§4).

---

## 4. How to deploy & test

Deploy stays `source_code_path` (dev). Per memory gotcha #3, the **user's own**
`databricks bundle deploy --target dev` works — no need for the `/opt/homebrew/bin` path.

### Slice 7A
```bash
# jobs.yml + the notebook are picked up by include: resources/*.yml — deploy uploads both.
databricks bundle deploy --target dev
# Run the job directly (proves it before any app wiring):
databricks bundle run forward_etl --target dev
```
Then verify:
- **Seed data:** either add a note + override through the deployed app (T3-3C), or INSERT a test
  row straight into `customer_notes_staging` / `customer_segment_overrides_staging` via psql.
- **After the run:** `SELECT count(*) FROM ai_27.lakebase_apps_capstone_gold.customer_notes`
  gained the row(s); `gold.customers.segment_id` for the overridden `customer_id` changed; the
  staging rows now show `processed=true`.
- **Idempotency:** run `bundle run forward_etl` again with no new rows → `notes_merged: 0,
  overrides_merged: 0`, gold rowcount unchanged (done-when: "re-running with no new staging rows
  is a no-op").
- Grab the job id for 7B: `databricks bundle summary --target dev` (or the job UI URL).

### Slice 7B
```bash
cd app/frontend && npm run build && cd ../..   # rebuild SPA (Reports page changed)
databricks bundle deploy --target dev
```
In an authenticated browser at
`https://customer360-984752964297111.11.azure.databricksapps.com/reports`:
- Click **Run forward-ETL** → a run id appears, status badge cycles RUNNING → SUCCESS (polling
  every ~3s, stops at terminal).
- The **recent-runs table** lists the run with a working "open in workspace" link.
- End-to-end: add a note via a customer's Notes tab → go to Reports → run → the note is now in
  `gold.customer_notes`.

### Optional pre-flight (catch typos before a slow deploy)
```bash
uv run python -c "import backend.main"                 # backend imports cleanly
databricks bundle validate --target dev                # jobs.yml + app.yml config sane
```

---

## 5. Done-when checklist (from CAPSTONE_TASKS.md T7)

- [ ] Triggering the job from the **Reports page** produces a successful run
- [ ] Re-running with **no new staging rows is a no-op** (Pattern A `processed=false` filter)
- [ ] `gold.customer_notes` rowcount equals the expected unique-note count in staging
      (rows with `processed=true`)
- [ ] (Ours, from D2) segment overrides `UPDATE gold.customers.segment_id`; re-applying the same
      value is idempotent
- [ ] (Ours) the app triggers the job as the **app SP** (CAN_MANAGE_RUN), not the user

---

## 6. Risks / gotchas specific to T7

- **No cross-system atomicity (§1.2):** don't claim MERGE + `processed` update are one
  transaction. Order = MERGE first, mark second; correctness comes from MERGE-on-key + the flag.
  Get this wrong (mark-then-merge) and a mid-run failure **loses** rows.
- **App SP lacks CAN_MANAGE_RUN (D3):** `run_now` → `PERMISSION_DENIED`. Fix = the `permissions:`
  block in `jobs.yml`. Verify after 7B deploy.
- **Serverless availability (D1):** if serverless notebook tasks are disabled, the job fails to
  start — fall back to a single-node job cluster. Resolve on the first `bundle run`.
- **`FORWARD_ETL_JOB_ID` unset (D5):** the router must 503 with a clear message, not 500.
  Remember to re-copy the id if the job is deleted/recreated (its id changes) — same class of
  issue as the git-cred re-registration in T8.
- **Dev-mode job name prefix:** `mode: development` prefixes the job name (`[dev abhishek_iyer]
  customer360_forward_etl`). Trigger by **id** (not name) to avoid surprises.
- **`created_at` type across systems:** Postgres `TIMESTAMPTZ` → Spark; ensure the DataFrame
  column lands as `TIMESTAMP` so the gold schema matches (cast if needed).
- **SDK version in the job (reverse-ETL gotcha #4):** keep the `%pip install --upgrade
  databricks-sdk` cell — `w.database.generate_database_credential` needs a recent SDK.
- **Metrics/analytics don't auto-reflect overrides:** overrides update `gold.customers`, but the
  app's live segment reads come from `customers_synced` (CONTINUOUS) — the synced table refreshes
  within seconds, so the UI will catch up. Worth a sentence in the reflection (round-trip:
  override → staging → gold → synced → UI).

---

## 7. Files touched (summary)

**New:**
- `lakebase/forward_etl/pattern_a_psycopg2/forward_etl_merge.py` (the job notebook — 7A)
- `resources/jobs.yml` (DABs job + app-SP CAN_MANAGE_RUN — 7A)
- `app/backend/routers/jobs.py` (trigger/poll/history — 7B)
- `app/frontend/src/pages/Reports.tsx` (7B)

**Edited:**
- `app/backend/config.py` (+`FORWARD_ETL_JOB_ID`), `app/app.yaml` (+env),
  `app/backend/models.py` (+`JobRun`/`JobRunTriggered`), `app/backend/main.py`
  (+`include_router(jobs.router)`), `app/frontend/src/api/client.ts` (+job fns/types),
  `app/frontend/src/App.tsx` (`/reports` → `Reports`).

**Unchanged but relied on:** `resources/app.yml` (T6 adds the `valueFrom` job binding),
`databricks.yml` (already `include: resources/*.yml`), `db.py`/`auth.py` (SP path reused).

---

## 8. Next after T7
→ **T4** Dashboard embed (low-risk iframe; needs the embed allowlist — master_plan §10 open
item). Then T5 Genie, T6 `app.yaml` finalize (incl. the `valueFrom` upgrade for this job id),
T8-full git-source, T3a external M2M, T9 ops, Optimizations. (master_plan §5 rows 8→14.)
