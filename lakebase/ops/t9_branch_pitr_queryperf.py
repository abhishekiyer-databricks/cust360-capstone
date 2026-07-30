#!/usr/bin/env python3
"""
T9 — Lakebase ops: branching, PITR, query insights.

Runs the whole task as one reproducible, self-cleaning script against LIVE
Lakebase, WITHOUT ever mutating prod (parent) data.

Model note: our instance `ai27-lb-apps-capstone` is a FLAT *Database Instance*
(not the Postgres-Projects model), so a "branch" is a CHILD INSTANCE created via
`parent_instance_ref`, and PITR = a child created at `parent_instance_ref.branch_time`.

Two platform constraints discovered live (2026-07-30) that shape the design:
  1. NESTED children are disabled — cannot create a child from another child.
     → a PITR restore must root at a TOP-LEVEL instance, not off a branch.
  2. `force` delete is NOT supported — delete children before their parent.

So we split the two skills onto the appropriate substrate:

  T9a — Branching + isolation → against REAL PROD data.
        Branch prod → B1 (current PIT). DELETE on B1 (blast-radius = branch only).
        Prove B1=0 while parent is UNCHANGED. Delete B1. Prod never mutated.

  T9b — Genuine PITR recovery + query insights → on a DEDICATED THROWAWAY
        top-level instance (ai27-lb-t9-demo) where destructive ops are safe:
          * create a table, insert N rows, capture T0
          * DELETE the rows (data really gone on this lineage)
          * PITR child @ T0 → rows RECOVERED (proves point-in-time restore)
          * seed ~200k audit rows, measure p95 of a point lookup with NO index
            (Seq Scan) then WITH index (Index Scan); report before/after.

Auth: connect as the current USER (full email) with a per-instance minted DB
credential. All destructive SQL is guarded by a host assertion so it can never
run against prod.

Teardown: delete every instance this script created; prod-adjacent instances
are left untouched. Set KEEP_DEMO=1 to leave ai27-lb-t9-demo up between runs.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from databricks.sdk import WorkspaceClient
from databricks.sdk.service import database as db

# ----------------------------------------------------------------------------- config
PROFILE = "DEFAULT"

# T9a — real prod lineage (READ + branch only; prod itself never mutated)
PARENT = "ai27-lb-apps-capstone"
PARENT_DB = "cust360ai27"
BRANCH_NAME = "ai27-lb-apps-capstone-branch"   # B1 (branch of prod)

# T9b — dedicated throwaway top-level instance (safe to delete/restore on)
DEMO = "ai27-lb-t9-demo"
DEMO_DB = "databricks_postgres"                # default db on a fresh instance
DEMO_PITR = "ai27-lb-t9-demo-pitr"             # PITR child of the demo

CAPACITY = "CU_1"
PROVISION_TIMEOUT = timedelta(minutes=20)

DEMO_NOTES_ROWS = 500                          # rows we insert then delete then restore
SEED_ROWS = 200_000
N_ACTORS = 500
TARGET_EMAIL = "user42@acme.example"
N_RUNS = 100
KEEP_DEMO = os.environ.get("KEEP_DEMO") == "1"

w = WorkspaceClient(profile=PROFILE)
ME = w.current_user.me().user_name
PARENT_HOST = w.database.get_database_instance(name=PARENT).read_write_dns


# ----------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(msg, flush=True)


def mint_token(instance_name: str) -> str:
    return w.database.generate_database_credential(
        request_id=str(uuid.uuid4()), instance_names=[instance_name]
    ).token


def connect(host: str, instance_name: str, dbname: str) -> psycopg.Connection:
    return psycopg.connect(
        host=host, port=5432, dbname=dbname,
        user=ME, password=mint_token(instance_name), sslmode="require",
        autocommit=True,
    )


def scalar(conn: psycopg.Connection, sql: str, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def instance_exists(name: str) -> bool:
    try:
        w.database.get_database_instance(name=name)
        return True
    except Exception:  # noqa: BLE001
        return False


def get_host(name: str) -> str:
    return w.database.get_database_instance(name=name).read_write_dns


def create_instance(name: str, parent_name: str | None = None,
                    branch_time: datetime | None = None) -> str:
    """Create a top-level instance, a branch, or a PITR child. Returns host."""
    ref = None
    if parent_name is not None:
        ref = db.DatabaseInstanceRef(name=parent_name)
        if branch_time is not None:
            ref.branch_time = branch_time.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
    spec = db.DatabaseInstance(name=name, capacity=CAPACITY, parent_instance_ref=ref)
    if parent_name is None:
        kind = "standalone instance"
    elif branch_time is not None:
        kind = f"PITR child @ {ref.branch_time}"
    else:
        kind = "branch"
    log(f"  creating {kind} '{name}'"
        + (f" from '{parent_name}'" if parent_name else "") + " …")
    inst = w.database.create_database_instance(spec).result(timeout=PROVISION_TIMEOUT)
    log(f"  '{name}' state={inst.state} host={inst.read_write_dns}")
    return inst.read_write_dns


def delete_instance(name: str) -> None:
    if not instance_exists(name):
        return
    try:
        w.database.delete_database_instance(name=name)  # no force (unsupported)
        log(f"  deleted '{name}'")
    except Exception as exc:  # noqa: BLE001
        log(f"  (delete '{name}' failed: {exc})")


# ----------------------------------------------------------------------------- p95 bench
@dataclass
class BenchResult:
    plan: str
    # server-side execution time (from EXPLAIN ANALYZE) — network-independent
    srv_p50_ms: float
    srv_p95_ms: float
    srv_max_ms: float
    # client-side wall-clock (includes network RTT laptop→Azure) — for reference
    cli_p50_ms: float
    cli_p95_ms: float


def _p95(sorted_samples: list[float]) -> float:
    return sorted_samples[min(len(sorted_samples) - 1,
                              int(0.95 * len(sorted_samples)))]


def bench(conn: psycopg.Connection, label: str) -> BenchResult:
    """Measure the point lookup 100×.

    Headline metric = SERVER-SIDE execution time via `EXPLAIN (ANALYZE)`
    ("Execution Time: N ms"), which isolates query work from the ~300ms
    laptop→Azure round-trip that otherwise swamps a fast point query. We also
    keep client-side wall-clock for reference/contrast.
    """
    sql = "SELECT * FROM customer_audit_log WHERE actor_email = %s"
    params = (TARGET_EMAIL,)
    explain_sql = "EXPLAIN (ANALYZE, TIMING ON, FORMAT JSON) " + sql

    plan = "?"
    srv: list[float] = []   # server "Execution Time" (ms), network-independent
    cli: list[float] = []   # client wall-clock (ms), includes RTT
    with conn.cursor() as cur:
        for _ in range(N_RUNS):
            t0 = time.perf_counter()
            cur.execute(explain_sql, params)
            root = cur.fetchone()[0][0]  # FORMAT JSON → [ { "Plan": …, "Execution Time": … } ]
            cli.append((time.perf_counter() - t0) * 1000.0)
            srv.append(float(root["Execution Time"]))
            plan = root["Plan"]["Node Type"]
    srv.sort()
    cli.sort()
    res = BenchResult(
        plan=plan,
        srv_p50_ms=statistics.median(srv), srv_p95_ms=_p95(srv), srv_max_ms=srv[-1],
        cli_p50_ms=statistics.median(cli), cli_p95_ms=_p95(cli),
    )
    log(f"  {label:6s} plan={res.plan:12s} "
        f"SERVER p50={res.srv_p50_ms:7.3f} p95={res.srv_p95_ms:7.3f} "
        f"max={res.srv_max_ms:7.3f} ms  | client p95={res.cli_p95_ms:7.1f} ms(+RTT)")
    return res


def pg_stat_snapshot(conn: psycopg.Connection, label: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT calls, round(mean_exec_time::numeric,3), "
                "round(max_exec_time::numeric,3) FROM pg_stat_statements "
                "WHERE query LIKE '%customer_audit_log%actor_email%' "
                "ORDER BY calls DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row:
            log(f"  {label:6s} pg_stat_statements: calls={row[0]} "
                f"mean={row[1]}ms max={row[2]}ms")
        else:
            log(f"  {label:6s} pg_stat_statements: (no matching row)")
    except Exception as exc:  # noqa: BLE001
        log(f"  {label:6s} pg_stat_statements unavailable: {exc}")


def try_reset_pg_stat(conn: psycopg.Connection) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_stat_statements_reset()")
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------------- T9a
def run_t9a() -> dict:
    log("\n" + "#" * 78)
    log("# T9a — BRANCHING + ISOLATION (against real prod, prod never mutated)")
    log("#" * 78)

    with connect(PARENT_HOST, PARENT, PARENT_DB) as p:
        n_parent = scalar(p, "SELECT count(*) FROM customer_notes_staging")
    log(f"[A1] parent customer_notes_staging count N = {n_parent}")

    delete_instance(BRANCH_NAME)  # clear any stale branch from a prior run
    b1_host = create_instance(BRANCH_NAME, parent_name=PARENT)
    assert b1_host != PARENT_HOST, "GUARD: branch host must differ from parent!"

    with connect(b1_host, BRANCH_NAME, PARENT_DB) as b1:
        n_b1 = scalar(b1, "SELECT count(*) FROM customer_notes_staging")
    log(f"[A2] B1 (branch) count = {n_b1}  (== N ? {n_b1 == n_parent})")

    log("[A3] destructive DELETE on B1 (branch only)")
    with connect(b1_host, BRANCH_NAME, PARENT_DB) as b1:
        assert b1_host != PARENT_HOST, "GUARD: refusing DELETE on parent host!"
        with b1.cursor() as cur:
            cur.execute("DELETE FROM customer_notes_staging")
        n_b1_after = scalar(b1, "SELECT count(*) FROM customer_notes_staging")
    log(f"      B1 count after DELETE = {n_b1_after}")

    with connect(PARENT_HOST, PARENT, PARENT_DB) as p:
        n_parent_now = scalar(p, "SELECT count(*) FROM customer_notes_staging")
    isolated = n_parent_now == n_parent
    log(f"[A4] parent count still = {n_parent_now}  "
        f"→ ISOLATION {'OK' if isolated else 'FAILED'}")

    delete_instance(BRANCH_NAME)
    return {"N": n_parent, "b1": n_b1, "b1_after": n_b1_after,
            "parent_after": n_parent_now, "isolated": isolated}


# ----------------------------------------------------------------------------- T9b
def run_t9b(demo_host: str) -> dict:
    log("\n" + "#" * 78)
    log("# T9b — GENUINE PITR RECOVERY + QUERY INSIGHTS (throwaway instance)")
    log("#" * 78)

    # --- set up a table on the demo, insert N, capture T0, then DELETE -------
    log("[P1] create table + insert rows on demo, capture T0, then DELETE")
    with connect(demo_host, DEMO, DEMO_DB) as d:
        with d.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS customer_notes_staging ("
                "note_id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
                "customer_id VARCHAR(20) NOT NULL, note_text TEXT NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            cur.execute("TRUNCATE customer_notes_staging")
            cur.execute(
                "INSERT INTO customer_notes_staging (customer_id, note_text) "
                "SELECT 'C'||lpad((g%%10000)::text,7,'0'), 'note '||g "
                "FROM generate_series(1,%s) g", (DEMO_NOTES_ROWS,),
            )
        n_demo = scalar(d, "SELECT count(*) FROM customer_notes_staging")
    # Sleep BEFORE capturing T0 so that even after we floor branch_time to whole
    # seconds, the restore point still lands comfortably AFTER the CREATE TABLE +
    # INSERT above (otherwise a sub-second floor can restore to before the table
    # existed → "relation does not exist").
    time.sleep(5)
    with connect(demo_host, DEMO, DEMO_DB) as d:
        t0 = scalar(d, "SELECT now()")
    log(f"      inserted N={n_demo}; T0={t0.astimezone(timezone.utc).isoformat()}")

    time.sleep(12)  # ensure branch_time is safely past + post-T0 WAL exists
    log("      DELETE FROM customer_notes_staging (data really gone on this lineage)")
    with connect(demo_host, DEMO, DEMO_DB) as d:
        assert demo_host != PARENT_HOST, "GUARD: refusing DELETE on parent host!"
        with d.cursor() as cur:
            cur.execute("DELETE FROM customer_notes_staging")
        n_demo_after = scalar(d, "SELECT count(*) FROM customer_notes_staging")
    log(f"      demo count after DELETE = {n_demo_after}")

    # --- PITR: create child of the demo @ T0 → rows recovered ----------------
    log("[P2] PITR child of demo @ T0")
    delete_instance(DEMO_PITR)  # clear stale
    pitr_host = create_instance(DEMO_PITR, parent_name=DEMO, branch_time=t0)
    with connect(pitr_host, DEMO_PITR, DEMO_DB) as pr:
        n_recovered = scalar(pr, "SELECT count(*) FROM customer_notes_staging")
    recovered = n_recovered == n_demo
    log(f"      PITR child count = {n_recovered}  "
        f"→ RECOVERY {'OK' if recovered else 'FAILED'} (== N={n_demo})")

    # --- query insights on the demo (seed 200k audit rows) -------------------
    log("[Q1] seed synthetic audit rows on demo")
    with connect(demo_host, DEMO, DEMO_DB) as d:
        with d.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS customer_audit_log ("
                "audit_id BIGSERIAL PRIMARY KEY, customer_id VARCHAR(20) NOT NULL, "
                "action VARCHAR(50) NOT NULL, actor_email VARCHAR(200) NOT NULL, "
                "payload JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            cur.execute("TRUNCATE customer_audit_log")
            cur.execute(
                "INSERT INTO customer_audit_log (customer_id, action, actor_email, payload) "
                "SELECT 'C'||lpad((g%%10000)::text,7,'0'), "
                "(ARRAY['add_note','override_segment'])[1+(g%%2)], "
                "'user'||(g%%%s)||'@acme.example', '{}'::jsonb "
                "FROM generate_series(1,%s) g", (N_ACTORS, SEED_ROWS),
            )
            cur.execute("ANALYZE customer_audit_log")
        total = scalar(d, "SELECT count(*) FROM customer_audit_log")
        target_ct = scalar(
            d, "SELECT count(*) FROM customer_audit_log WHERE actor_email=%s",
            (TARGET_EMAIL,))
    log(f"      total audit rows = {total}; target '{TARGET_EMAIL}' → {target_ct} rows")

    has_pgss = False
    with connect(demo_host, DEMO, DEMO_DB) as d:
        try:
            with d.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
            has_pgss = True
            log("      pg_stat_statements enabled")
        except Exception as exc:  # noqa: BLE001
            log(f"      pg_stat_statements not enabled (client-side p95 only): {exc}")

    log("[Q2] BEFORE — no index on actor_email")
    with connect(demo_host, DEMO, DEMO_DB) as d:
        if has_pgss:
            try_reset_pg_stat(d)
        before = bench(d, "before")
        if has_pgss:
            pg_stat_snapshot(d, "before")

    log("[Q3] CREATE INDEX on customer_audit_log(actor_email)")
    with connect(demo_host, DEMO, DEMO_DB) as d:
        with d.cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor_email "
                        "ON customer_audit_log (actor_email)")
            cur.execute("ANALYZE customer_audit_log")
        log("      index created + analyzed")

    log("[Q4] AFTER — with index")
    with connect(demo_host, DEMO, DEMO_DB) as d:
        if has_pgss:
            try_reset_pg_stat(d)
        after = bench(d, "after")
        if has_pgss:
            pg_stat_snapshot(d, "after")

    delete_instance(DEMO_PITR)
    return {"N": n_demo, "demo_after": n_demo_after, "recovered_count": n_recovered,
            "recovered": recovered, "before": before, "after": after,
            "seed_total": total, "target_ct": target_ct}


# ----------------------------------------------------------------------------- main
def main() -> int:
    log("=" * 78)
    log(f"T9 Lakebase ops — user={ME}")
    log(f"prod parent={PARENT} host={PARENT_HOST}")
    log(f"demo instance={DEMO}")
    log("=" * 78)

    a = b = None
    try:
        a = run_t9a()

        if not instance_exists(DEMO):
            demo_host = create_instance(DEMO)
        else:
            demo_host = get_host(DEMO)
            log(f"\n(reusing existing demo instance {DEMO} host={demo_host})")
        b = run_t9b(demo_host)

    finally:
        log("\n" + "#" * 78)
        log("# TEARDOWN")
        log("#" * 78)
        delete_instance(BRANCH_NAME)
        delete_instance(DEMO_PITR)
        if KEEP_DEMO:
            log(f"  KEEP_DEMO=1 → leaving '{DEMO}' up")
        else:
            delete_instance(DEMO)
        # confirm none of OUR instances linger
        ours = [n for n in (BRANCH_NAME, DEMO_PITR, DEMO)
                if instance_exists(n) and not (n == DEMO and KEEP_DEMO)]
        prod_ok = instance_exists(PARENT)
        log(f"  our leftover instances: {ours or 'none'}")
        log(f"  prod '{PARENT}' intact: {prod_ok}")

    # ---- summary ------------------------------------------------------------
    log("\n" + "=" * 78)
    log("T9 RESULTS")
    log("=" * 78)
    if a:
        log("T9a  branching + isolation (real prod):")
        log(f"     branch B1 = {a['b1']} (==N={a['N']}); DELETE→{a['b1_after']}; "
            f"parent stayed {a['parent_after']} → "
            f"ISOLATION {'PROVEN' if a['isolated'] else 'FAILED'}")
    if b:
        log("T9b  PITR recovery (throwaway instance):")
        log(f"     inserted N={b['N']}; DELETE→{b['demo_after']}; "
            f"PITR@T0→{b['recovered_count']} → "
            f"{'RECOVERED' if b['recovered'] else 'FAILED'}")
        log("T9b  query insights (server-side p95, network-independent):")
        log(f"     seeded {b['seed_total']} rows; target matches {b['target_ct']}")
        bf, af = b['before'], b['after']
        log(f"     before plan={bf.plan:12s} p95={bf.srv_p95_ms:8.3f} ms "
            f"(client p95 {bf.cli_p95_ms:.0f} ms incl. RTT)")
        log(f"     after  plan={af.plan:12s} p95={af.srv_p95_ms:8.3f} ms "
            f"(client p95 {af.cli_p95_ms:.0f} ms incl. RTT)")
        sp = bf.srv_p95_ms / af.srv_p95_ms if af.srv_p95_ms else float('inf')
        log(f"     speedup ≈ {sp:.1f}× (server-side p95)")
    log("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
