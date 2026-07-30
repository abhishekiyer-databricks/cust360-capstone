# T9 — Lakebase ops: branching, PITR, query insights

> **Status:** ✅ **DONE + verified 2026-07-30.** Style matches `t3_plan.md` / `t7_plan.md` /
> `t3a_plan.md`: concept → design decisions → step-by-step → run/verify → done-when.
>
> **⚠️ Design changed vs. §2 below after two live platform constraints surfaced during
> execution** — the as-built approach is authoritative; §2's original lineage is left for the
> reasoning trail:
> 1. **Nested children are disabled** ("Cannot create a child instance from another child") →
>    the planned parent→B1→B2 PITR lineage (D-T9.2) is impossible. A PITR restore must root at
>    a **top-level** instance.
> 2. **`force` delete is unsupported** → delete children before their parent (no `--force`).
>
> **As-built split:** **T9a (branch + isolation)** runs against **real prod** (branch prod →
> `DELETE` on branch → prod unchanged). **T9b (genuine PITR recovery + query perf)** runs on a
> **dedicated throwaway top-level instance** `ai27-lb-t9-demo` (insert 500 → `DELETE` → PITR
> child @ T0 → 500 recovered; then seed 200k audit rows → index before/after). This is a
> *cleaner* demo than D-T9.3's "seed on the branch" idea and keeps prod pristine.
> Script: `lakebase/ops/t9_branch_pitr_queryperf.py`; output: `lakebase/ops/t9_run_output.txt`.
>
> **Headline numbers:** isolation proven (branch 7→0, prod stayed 7); PITR recovered 500/500;
> query perf **Seq Scan 16.4 ms → Bitmap/Index Scan 0.89 ms p95 ≈ 18×** (server-side, matches
> `pg_stat_statements` 13.7→1.08 ms). All instances torn down; prod intact.
> **Prereq context:** T1 (Lakebase instance + synced/staging tables), T7/T3a (forward-ETL job
> populating `customer_audit_log`) all DONE. This task does NOT touch app code — it's pure
> **Lakebase infrastructure ops** run from the CLI + psql against the live instance.
> **Submission note:** the task doc's done-when says "screenshots"; the user is **not
> submitting screenshots/recordings**, so our done-when is the *captured numbers* instead —
> verified restored row counts and before/after p95 latency printed to the terminal / recorded
> in `process_doc.md`.

---

## 0. Where this sits

| | What we've built | **T9 (this task)** |
|---|---|---|
| Focus | App features (read/write/ETL/auth/deploy) | **Database operations** on the Lakebase instance itself |
| Touches app code? | Yes | **No** — CLI + SQL only |
| Risk surface | App runtime | **The live prod instance** (`ai27-lb-apps-capstone`) backing the running app + 3 CONTINUOUS/TRIGGERED synced tables |

T9 has two independent sub-tasks:

- **T9a — Branch + PITR:** create a child branch of the instance, do a destructive delete in
  isolation, and recover data to a point-in-time.
- **T9b — Query insights:** show a point-lookup is slow without an index, add the index,
  measure the before/after p95 improvement.

---

## 1. Concept — what this teaches

### 1.1 Lakebase branching (copy-on-write database clones)

A **branch** in Lakebase is a **child Database Instance** created from a parent at a
**point in time**. It's a copy-on-write clone: instant to create (no physical data copy up
front), fully writable, and **completely isolated** from the parent. Write to the branch →
the parent is untouched; write to the parent → the branch is untouched. This is the "git
branch for your database" model: spin up a throwaway copy of prod data to test a destructive
migration, a bulk backfill, or an index change, then throw it away.

> **Two Lakebase resource models — know which one we're on.** The newer *Lakebase
> Autoscaling / Postgres Projects* model (`databricks postgres create-branch
> projects/…/branches/…`) has first-class named branches. **Our instance is the older flat
> *Database Instances* model** (`databricks database …` / `/api/2.0/database/instances`),
> where a "branch" is literally a **child instance** created via `parent_instance_ref`.
> We use the Database-Instances API throughout T9. (Verified live: `postgres` CLI exists but
> targets `projects/*`; our instance `ai27-lb-apps-capstone` is a flat instance with
> `read_write_dns`, `uid`, `retention_window_in_days`.)

### 1.2 Point-in-time restore (PITR)

Every Database Instance keeps a **restore window** (retention, 2–35 days, **ours = 7**). PITR
= create a child instance whose `parent_instance_ref.branch_time` is a **past UTC timestamp
inside that window** (or a WAL `lsn`). The child comes up holding the parent's data **exactly
as it was at that instant**. That's how you recover from "someone deleted the wrong rows at
14:32": branch the parent as of 14:31 and read the good data back out.

**Key mental model:** Lakebase PITR does **not** roll the parent back in place. It **produces
a new instance** with the historical state. "Restore" = "branch at a past timestamp, then read
(or copy) the recovered rows." This matters for how we phrase T9a below.

### 1.3 Query insights / the missing-index story

Postgres logs per-statement stats in **`pg_stat_statements`** (mean/max/stddev exec time,
call count). The **Query Performance UI** in the Lakebase console visualizes p50/p95/p99 per
normalized query. A point lookup like `WHERE actor_email = '…'` on an **unindexed** column
forces a **sequential scan** — O(rows) per query. Add a B-tree index on that column → the
planner switches to an **index scan** — O(log rows). T9b makes that difference *measurable*.

---

## 2. Design decisions (options → pick → why)

### D-T9.1 — Do the destructive `DELETE` **on the branch, never on the parent.** 🔒

The task text literally says *"On the branch, `DELETE FROM customer_notes_staging`
(destructive). On the parent, restore to a timestamp before the delete."* Read carefully: the
**destructive op belongs on the branch**, and PITR is the recovery mechanism. Our parent is
the **live prod instance** — it backs the running app and 3 synced tables. **We never run a
destructive statement against the parent.** The branch is the blast-radius container. This is
*exactly* the value prop branching exists to demonstrate: prove isolation by nuking the branch
copy while prod keeps serving.

- **Rejected:** deleting on the parent then PITR-restoring the parent. In-place parent
  rollback isn't the Database-Instance model anyway (PITR makes a *new* instance), and it would
  put the live app at risk. No.

### D-T9.2 — PITR demonstrated as a **point-in-time child** (the honest mechanism). 🔒

Since "restore" means "branch at a past timestamp," we demonstrate recovery by capturing a
timestamp **T0 before the destructive delete**, doing the delete on the branch, then creating a
**second** point-in-time child at `branch_time = T0` and showing the row count is back to the
original N. That's a faithful, provable PITR: *deleted on a live-ish copy → recovered exact
historical state at a timestamp.*

> **"Only one child at a time" constraint (verified in docs).** A Database Instance can have
> **one** child at a time, and a parent can't be deleted while a child exists. So we run the
> two children **sequentially off the parent** and delete each before creating the next:
> 1. Child **B1** = "the branch" (current PIT) → do the destructive delete here.
> 2. Delete B1.
> 3. Child **B2** = "the PITR restore" (`branch_time = T0`) → read the recovered rows.
> This keeps us within the one-child rule and each child's role is crisp. (Chaining B2 as a
> child *of B1* is possible in principle but adds a fragile dependency on child-of-child
> retention windows — we avoid it.)

### D-T9.3 — Seed a **large synthetic `customer_audit_log` on the branch** for T9b. 🔒

Prod `customer_audit_log` has **~16 rows** (real app writes). A point query on 16 rows is
sub-millisecond **with or without an index** — the missing-index story would show *nothing*.
To make the sequential scan actually hurt, we **bulk-insert ~200k synthetic audit rows**, then
measure. Doing this **on the T9a branch** (not prod) means:
- prod `customer_audit_log` stays pristine (no junk rows to clean up),
- we get a realistic dataset where the seq scan is clearly slow,
- teardown is free — deleting the branch discards all seeded data.

So T9a's branch is **reused for T9b**. One branch, both sub-tasks, zero prod impact.

- **Rejected:** seeding prod then `DELETE`-ing the junk afterward — risk of leaving debris in a
  live table, and concurrent app writes muddy the measurement. Branch is cleaner.

### D-T9.4 — Measure p95 **client-side over 100 runs** + corroborate with `pg_stat_statements`.

The task says "record before/after p95 latency." Two complementary sources:
- **Client-side:** run the query **100×**, collect 100 wall-clock samples, compute p95 in
  Python (`statistics.quantiles`). This is the headline before/after number.
- **`pg_stat_statements`:** `SELECT query, calls, mean_exec_time, max_exec_time FROM
  pg_stat_statements WHERE query LIKE '%customer_audit_log%'` — server-side confirmation
  (mean/max) and proof the query normalized to one plan. Call `pg_stat_statements_reset()`
  between the before and after runs so the two windows don't blend.

> The **Query Performance UI** shows p95 natively; since we're not submitting screenshots, the
> printed client-side p95 + pg_stat_statements rows ARE the record. (Optionally still open the
> UI once to see it — nice for understanding, not required for done-when.)

### D-T9.5 — Connect as the **user**, minted credential, `CU_1` children, **delete promptly.**

- **Auth:** connect to the branch instances as the **current user** (full email
  `w.current_user.me().user_name`), password = a freshly minted DB credential:
  `w.database.generate_database_credential(request_id=<uuid>, instance_names=[<branch_name>]).token`.
  This is the **"Local Lakebase check trick"** already in project memory — bare `abhishek.iyer`
  (no domain) fails password auth; the SP path isn't needed for ops we run ourselves.
  **Each child instance needs its OWN credential** (mint per `instance_names=[branch]`).
- **Capacity:** create children at **`CU_1`** (cheapest) — this is throwaway ops, not
  serving.
- **Cost hygiene:** child instances **bill while alive.** Delete B1 before creating B2, and
  delete B2 the moment T9 numbers are captured. End state = **only the parent exists**, exactly
  as before T9.

---

## 3. Reference facts (verified this session)

| Thing | Value |
|---|---|
| Parent instance name | `ai27-lb-apps-capstone` |
| Parent uid | `eb9d220d-ebe6-42d8-8113-c9585a3a7166` |
| Parent read_write_dns | `ep-plain-art-e1jje7ek.database.eastus2.azuredatabricks.net` |
| PG database | `cust360ai27` |
| Retention window | **7 days** (PITR `branch_time` must be within last 7d) |
| Capacity | `CU_1` |
| Create-branch endpoint | `POST /api/2.0/database/instances` with `parent_instance_ref` |
| PITR field | `parent_instance_ref.branch_time` (ISO-8601 UTC) or `.lsn` |
| Delete w/ descendants | `delete-database-instance --force` |
| CLI to use | **`/opt/homebrew/bin/databricks` v1.5.0** (gotcha #14 — NOT the PATH v0.291.0) |
| Profile | `DEFAULT` (re-auth if OAuth expired: `databricks auth login --profile DEFAULT`) |

Branch request body (confirmed shape):
```json
{ "name": "ai27-lb-apps-capstone-branch", "capacity": "CU_1",
  "parent_instance_ref": { "name": "ai27-lb-apps-capstone" } }
```
PITR request body:
```json
{ "name": "ai27-lb-apps-capstone-pitr", "capacity": "CU_1",
  "parent_instance_ref": { "name": "ai27-lb-apps-capstone",
                           "branch_time": "2026-07-30T21:30:00Z" } }
```

---

## 4. Step-by-step implementation

Everything runs from a **single Python script** we'll write once so the run is reproducible and
the numbers are captured cleanly: `lakebase/ops/t9_branch_pitr_queryperf.py`. It uses the
Databricks SDK (`WorkspaceClient`) for instance lifecycle + credential minting and `psycopg`
for SQL. (We can also run the CLI commands by hand — both documented below — but a script makes
the p95 measurement and teardown deterministic.)

> New dir `lakebase/ops/` (sibling to `reverse_etl/` and `forward_etl/`). This is an ops
> artifact, not app code — it never ships in the app bundle.

### Phase A — T9a: branch + destructive delete + PITR

**A1. Capture baseline.** Connect to the **parent**, record:
- `N = SELECT count(*) FROM customer_notes_staging;`
- `T0 =` now (UTC), captured *before* any delete — this is our PITR target.
  (Sleep ~5–10s after T0 so `branch_time` is safely in the past and distinct.)

**A2. Create the branch B1** (`ai27-lb-apps-capstone-branch`, current PIT):
```bash
/opt/homebrew/bin/databricks database create-database-instance ai27-lb-apps-capstone-branch \
  --json '{"capacity":"CU_1","parent_instance_ref":{"name":"ai27-lb-apps-capstone"}}' \
  -p DEFAULT
```
Wait for `state == AVAILABLE` (SDK waits by default; CLI waits unless `--no-wait`). Grab B1's
`read_write_dns`.
- **Verify branch isolation baseline:** connect to **B1** (own minted credential), confirm
  `count(*) FROM customer_notes_staging == N` (branch starts as an exact copy).

**A3. Destructive delete on B1 (NOT parent):**
```sql
DELETE FROM customer_notes_staging;   -- run against B1's host only
```
- B1 count → **0**.
- Re-check **parent** count → still **N**. ✅ **Isolation proven** (destructive op contained
  to the branch; live app unaffected).

**A4. Delete B1** to free the single-child slot:
```bash
/opt/homebrew/bin/databricks database delete-database-instance ai27-lb-apps-capstone-branch --force -p DEFAULT
```

**A5. PITR — create B2 at `branch_time = T0`:**
```bash
/opt/homebrew/bin/databricks database create-database-instance ai27-lb-apps-capstone-pitr \
  --json '{"capacity":"CU_1","parent_instance_ref":{"name":"ai27-lb-apps-capstone","branch_time":"<T0-ISO-UTC>"}}' \
  -p DEFAULT
```
- Connect to **B2**, `SELECT count(*) FROM customer_notes_staging;` → **== N**.
- ✅ **PITR proven:** data recovered to the exact pre-delete state at timestamp T0.
- *(Note for the writeup: because we deleted on the branch, the parent's `count` never changed
  — so B2's count also equals the parent's. The demonstration is that a **point-in-time child
  reproduces historical state on demand**. If we wanted a strict before/after contrast on one
  lineage, we'd delete on B1 then branch B2 **from B1** at T0; we chose the simpler off-parent
  path per D-T9.2 to respect the one-child rule and avoid child-of-child fragility. Call this
  out honestly in the reflection.)*

### Phase B — T9b: query insights (reuse a branch — see note)

Because B1 was deleted in A4, T9b uses **B2** (already up from A5) as the sandbox — or, if B2
was torn down, create a fresh single-purpose branch `ai27-lb-apps-capstone-qperf`. Either way,
**seed + measure on a branch, never prod** (D-T9.3).

> Sequencing tip: if we want one branch for both, reorder to keep B2 alive: run T9a through A5,
> **do T9b on B2**, then delete B2 last. The script will do exactly this.

**B1. Seed ~200k synthetic audit rows** on the branch:
```sql
INSERT INTO customer_audit_log (customer_id, actor_email, action, payload)
SELECT
  'C' || lpad((g % 10000)::text, 7, '0'),
  'user' || (g % 500) || '@acme.example',            -- 500 distinct actors
  (ARRAY['add_note','override_segment'])[1 + (g % 2)],
  '{}'::jsonb
FROM generate_series(1, 200000) g;
```
*(Match real column names/types — verify against the live staging DDL first; `payload` is
`jsonb`. If `customer_audit_log` has a NOT NULL/DEFAULT we're missing, adjust the column list.)*
- `ANALYZE customer_audit_log;` so the planner has fresh stats.
- Pick a `TARGET_EMAIL` that matches a known number of rows (e.g. `user42@acme.example`).

**B2. Confirm no index yet + confirm it's a seq scan:**
```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM customer_audit_log WHERE actor_email = '<TARGET_EMAIL>';
-- expect: Seq Scan on customer_audit_log
```

**B3. BEFORE measurement:**
- `SELECT pg_stat_statements_reset();`
- Run the point query **100×** in a loop, timing each (`time.perf_counter()` around
  `cur.execute(...); cur.fetchall()`).
- Compute **p95_before** from the 100 samples.
- Snapshot `pg_stat_statements` row for this query (`calls`, `mean_exec_time`, `max_exec_time`).

**B4. Add the index:**
```sql
CREATE INDEX idx_audit_actor_email ON customer_audit_log (actor_email);
ANALYZE customer_audit_log;
```
- Re-run `EXPLAIN (ANALYZE)` → expect **Index Scan using idx_audit_actor_email**.

**B5. AFTER measurement:** repeat B3 exactly (reset stats, 100 runs, p95_after,
`pg_stat_statements` snapshot).

**B6. Record the delta:** print a small table —
```
                p95 (ms)   mean (ms, pg_stat)   plan
  before          <x>            <y>            Seq Scan
  after           <a>            <b>            Index Scan
  improvement     <x/a>×
```

### Phase C — Teardown (mandatory, cost hygiene)

```bash
# delete whichever children remain (B2 / qperf); --force since parent has descendants
/opt/homebrew/bin/databricks database delete-database-instance ai27-lb-apps-capstone-pitr  --force -p DEFAULT
# (and any other child created)
/opt/homebrew/bin/databricks database list-database-instances -p DEFAULT -o json | jq '.[].name'
# → confirm ONLY ai27-lb-apps-capstone remains
```
End state must equal pre-T9 state: **parent only, no children, prod data untouched.**

---

## 5. Files touched

| File | New? | What |
|---|---|---|
| `lakebase/ops/t9_branch_pitr_queryperf.py` | new | one-shot script: baseline → branch → delete-on-branch → PITR child → seed+measure p95 before/after index → teardown; prints all captured numbers |
| `lakebase/ops/README.md` | new (optional) | how to run it + the recorded numbers |
| `process_doc.md` | edit | add a **T9** section: branching/PITR mechanics, isolation proof (parent count unchanged), PITR recovered count = N, p95 before/after table, the "PITR makes a new instance, not in-place rollback" teaching point, cost-hygiene teardown |
| `claude_plans/master_plan.md` | edit | tick T9 in §5 / §8 checklist |
| project memory | edit | append a T9 session entry |

**No app code, no `resources/*.yml`, no deploy.** T9 is infra ops only.

---

## 6. How to run & what "working" looks like

```bash
# 0. ensure fresh auth
databricks auth login --profile DEFAULT   # if token expired
# 1. deps (uv/pip): databricks-sdk, psycopg[binary]  (already in app venv; ops can reuse it)
# 2. run
python lakebase/ops/t9_branch_pitr_queryperf.py
```
Expected stdout (illustrative):
```
parent customer_notes_staging count N = 7   T0 = 2026-07-30T21:30:05Z
branch B1 AVAILABLE  host=ep-...   B1 count = 7   (== N ✓ copy)
B1 after DELETE: 0     parent still: 7   → ISOLATION ✓
deleted B1
PITR child B2 @ T0 AVAILABLE   B2 count = 7   → PITR RESTORE ✓ (== N)
seeded audit rows: 200000   target=user42@acme.example (matches 400 rows)
BEFORE  plan=Seq Scan   p95=41.7ms  mean(pg_stat)=38.2ms
CREATE INDEX idx_audit_actor_email ... done
AFTER   plan=Index Scan p95=1.9ms   mean(pg_stat)=1.1ms   → ~22× faster
teardown: deleted B2 ; remaining instances: ['ai27-lb-apps-capstone']  ✓
```

---

## 7. Done-when (adapted for no-screenshot submission)

- [x] **Branch created** from `ai27-lb-apps-capstone` and reached AVAILABLE (name + host printed).
- [x] **Isolation proven:** destructive `DELETE FROM customer_notes_staging` on the branch →
      branch count **0** while **parent count unchanged (= 7)**.
- [x] **PITR proven:** point-in-time child at `branch_time = T0` → `customer_notes_staging`
      count **= 500** (recovered pre-delete state, on the throwaway lineage). *(Replaces
      "post-restore row count" screenshot.)*
- [x] **Query perf:** point query on `customer_audit_log(actor_email)` shows **Seq Scan** before,
      **Bitmap/Index Scan** after `CREATE INDEX`; **p95 before/after** recorded **server-side**
      (16.4 → 0.89 ms ≈ 18×) + confirmed by `pg_stat_statements` (13.7 → 1.08 ms). *(Replaces
      "before/after p95 latency" screenshot; server-side metric chosen because client wall-clock
      is swamped by ~300ms Azure RTT — see gotcha in §8 / process_doc.)*
- [x] **Teardown:** all child instances deleted; `list-database-instances` shows no T9 leftovers,
      prod intact.
- [x] `process_doc.md` T9 section written (numbers + reflection points).

---

## 8. Risks / gotchas to watch

1. **Never target the parent host for writes.** The script must connect T9a-delete and T9b-seed
   to the **branch's** `read_write_dns`, not the parent's. Double-check the host string before
   any `DELETE`/`INSERT`. (Guard: assert host != parent host before destructive SQL.)
2. **Per-instance credentials.** `generate_database_credential(instance_names=[X])` mints a
   token scoped to instance X — mint a fresh one for each branch; a parent token won't auth to a
   child.
3. **`branch_time` window.** Must be within the last 7 days and in the past; format ISO-8601
   UTC with trailing `Z`. Too-recent (future/edge) timestamps get rejected.
4. **One child at a time.** Create B2 only after B1 is deleted (or branch B2 from B1) — the API
   rejects a second concurrent child of the same parent.
5. **Provisioning latency.** A child can take a few minutes to reach AVAILABLE. SDK waits; if
   using raw CLI, don't `--no-wait` unless you poll `get-database-instance` for state.
6. **Cost.** Children bill while alive. Teardown is part of done-when, not optional.
7. **Seed size vs. CU_1.** 200k rows on CU_1 is fine; if the seq scan still isn't slow enough to
   show a clean p95 gap, bump to 500k–1M rows (still trivial storage) rather than reducing runs.
8. **`pg_stat_statements` availability.** It's enabled on Lakebase; if the view is empty, ensure
   the extension is present (`CREATE EXTENSION IF NOT EXISTS pg_stat_statements;`) and that we
   reset between before/after so the two windows don't merge.
9. **CLI version:** all `database` commands via `/opt/homebrew/bin/databricks` (v1.5.0).
