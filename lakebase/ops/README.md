# Lakebase ops — T9 (branching, PITR, query insights)

One reproducible, self-cleaning script that exercises the three T9 Lakebase-ops skills
against the **live** workspace without ever mutating prod data.

## Run

```bash
# from repo root; uses the DEFAULT profile
databricks auth login --profile DEFAULT      # if the OAuth token has expired
app/.venv/bin/python lakebase/ops/t9_branch_pitr_queryperf.py
```

- `KEEP_DEMO=1` leaves the throwaway `ai27-lb-t9-demo` instance up between runs (faster
  re-runs; remember to delete it manually afterwards to stop billing).
- Runtime ≈ 5–12 min (each child instance takes a few minutes to reach AVAILABLE).

Captured output from the verified run is in [`t9_run_output.txt`](./t9_run_output.txt).

## What it does

Our instance `ai27-lb-apps-capstone` is the flat **Database Instance** model, so a "branch"
is a **child instance** (`parent_instance_ref`) and **PITR** is a child created at
`parent_instance_ref.branch_time`. Two platform constraints matter:

1. **Nested children are disabled** → a PITR restore must root at a top-level instance.
2. **`force` delete is unsupported** → delete children before their parent.

So the two skills run on the appropriate substrate:

| Phase | Substrate | Proves |
|---|---|---|
| **T9a** branch + isolation | **real prod** (`ai27-lb-apps-capstone`) | branch prod → `DELETE` on the branch → **prod row count unchanged** |
| **T9b** PITR recovery | throwaway `ai27-lb-t9-demo` | insert 500 → `DELETE` → PITR child @ T0 → **500 rows recovered** |
| **T9b** query insights | throwaway `ai27-lb-t9-demo` | seed 200k rows → point lookup **Seq Scan → Index Scan** after `CREATE INDEX` |

## Verified results (2026-07-30)

- **T9a isolation:** branch = 7 (= N), `DELETE` → 0, parent stayed **7** → isolation proven.
- **T9b PITR:** inserted 500, `DELETE` → 0, PITR child @ T0 → **500 recovered**.
- **T9b query perf (server-side p95):** `Seq Scan` **16.4 ms** → `Bitmap/Index Scan`
  **0.89 ms** ≈ **18×** (confirmed by `pg_stat_statements` mean 13.7 ms → 1.08 ms).

## Safety / cost notes

- Every destructive statement is guarded by `assert host != PARENT_HOST` — the script cannot
  run a `DELETE` against prod.
- Connects as the **current user** with a **per-instance minted** credential.
- Children are created at **CU_1** and **torn down at the end**; the script confirms no T9
  instances linger and prod is intact. Child instances **bill while alive** — do not leave
  them up.

## Why server-side latency (not client wall-clock)

A laptop→Azure round-trip (~300 ms) dominates and is identical before/after, so client-side
wall-clock hides the index gain (looked like ~1.3×). The script reports **server execution
time** from `EXPLAIN (ANALYZE, FORMAT JSON)` (`"Execution Time"`), which is network-independent
and matches `pg_stat_statements`.
