# Databricks notebook source
# MAGIC %md
# MAGIC # Optimizations O5 — composite index on `customers_synced` (feasibility + create + measure)
# MAGIC
# MAGIC The customer list endpoint filters by `segment_id` and orders by `lifetime_value DESC`
# MAGIC (master_plan §7). A composite index `(segment_id, lifetime_value DESC)` can serve both
# MAGIC the filter and the sort. BUT `customers_synced` is a **pipeline-managed synced table**
# MAGIC (reverse ETL from gold, T1) — so before relying on a secondary index we must check
# MAGIC whether the sync permits it and whether a resync keeps it.
# MAGIC
# MAGIC This notebook:
# MAGIC 1. **Feasibility** — inspect existing indexes; attempt to create the composite index.
# MAGIC 2. **Measure** — server-side `EXPLAIN (ANALYZE, FORMAT JSON)` before/after (GOTCHA #17:
# MAGIC    report the planner's `Execution Time`, NOT laptop→Azure wall-clock).
# MAGIC 3. **Document** — print a clear verdict for the reflection: durable / not-supported.
# MAGIC
# MAGIC Idempotent (`CREATE INDEX IF NOT EXISTS`). Connects as the current user via a fresh
# MAGIC Lakebase OAuth token (same pattern as 02/03). Run as a job (needs workspace network).

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade databricks-sdk psycopg2-binary

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("instance_name", "ai27-lb-apps-capstone", "Lakebase instance name")
dbutils.widgets.text("database_name", "cust360ai27", "Postgres database name")

INSTANCE = dbutils.widgets.get("instance_name")
DB_NAME = dbutils.widgets.get("database_name")

# COMMAND ----------

import json
import uuid

import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
instance = w.database.get_database_instance(name=INSTANCE)
host = instance.read_write_dns
me = spark.sql("SELECT current_user()").collect()[0][0]
token = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()), instance_names=[INSTANCE]
).token

print(f"connecting to {host}/{DB_NAME} as {me}")
conn = psycopg2.connect(
    host=host, port=5432, dbname=DB_NAME,
    user=me, password=token, sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()

TABLE = "customers_synced"
INDEX = "idx_customers_seg_ltv"

# COMMAND ----------

# MAGIC %md ## 1. Feasibility — existing indexes on customers_synced

# COMMAND ----------

cur.execute(
    "SELECT indexname, indexdef FROM pg_indexes "
    "WHERE schemaname = 'public' AND tablename = %s ORDER BY indexname",
    (TABLE,),
)
before_idx = cur.fetchall()
print(f"existing indexes on {TABLE}:")
for name, ddl in before_idx:
    print(f"  {name}: {ddl}")

# COMMAND ----------

# MAGIC %md ## 2. Measure BEFORE — server-side Execution Time for a representative list query

# COMMAND ----------

# Representative of routers/customers.py list_customers: filter by segment + order by LTV desc.
LIST_QUERY = (
    "SELECT customer_id, first_name, last_name, email, country, segment_id, "
    "lifetime_value, churn_score FROM customers_synced "
    "WHERE segment_id ILIKE 'S1' "
    "ORDER BY lifetime_value DESC NULLS LAST, customer_id LIMIT 25 OFFSET 0"
)


def exec_time_ms(query: str) -> float:
    cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {query}")
    plan = cur.fetchone()[0]  # list with one plan dict
    root = plan[0]
    return float(root["Execution Time"])


before_ms = exec_time_ms(LIST_QUERY)
print(f"BEFORE index: Execution Time = {before_ms:.3f} ms")

# COMMAND ----------

# MAGIC %md ## 3. Attempt to create the composite index (feasibility)

# COMMAND ----------

index_created = False
create_error = None
try:
    # CONCURRENTLY can't run in an autocommit-wrapped multi-statement txn in all setups, but
    # a single statement with autocommit=True is fine. If the synced-table target rejects
    # secondary indexes we catch it here and document the constraint.
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX} "
        f"ON {TABLE} (segment_id, lifetime_value DESC)"
    )
    index_created = True
    print(f"index {INDEX} created (or already existed).")
except Exception as exc:  # noqa: BLE001 — we want the message for the writeup
    create_error = str(exc)
    print(f"index creation NOT supported / failed: {create_error}")

# COMMAND ----------

# MAGIC %md ## 4. Measure AFTER (only meaningful if the index was created)

# COMMAND ----------

after_ms = None
scan_kind = None
if index_created:
    # ANALYZE so the planner has fresh stats and will consider the new index.
    cur.execute(f"ANALYZE {TABLE}")
    after_ms = exec_time_ms(LIST_QUERY)
    cur.execute(f"EXPLAIN (FORMAT JSON) {LIST_QUERY}")
    plan_text = json.dumps(cur.fetchone()[0])
    scan_kind = "Index/Bitmap Scan" if "Index" in plan_text or "Bitmap" in plan_text else "Seq Scan"
    print(f"AFTER index: Execution Time = {after_ms:.3f} ms  (plan uses: {scan_kind})")
    if before_ms > 0:
        print(f"speedup ≈ {before_ms / after_ms:.1f}×")

# COMMAND ----------

# MAGIC %md ## 5. Verdict for the reflection

# COMMAND ----------

verdict = {
    "table": TABLE,
    "index": INDEX,
    "index_supported": index_created,
    "create_error": create_error,
    "before_execution_ms": round(before_ms, 3),
    "after_execution_ms": round(after_ms, 3) if after_ms is not None else None,
    "plan_after": scan_kind,
    "note": (
        "Synced tables are pipeline-managed; if a resync drops this index, re-run this "
        "notebook or move the optimization to the gold Delta side. 10k rows is small enough "
        "that the missing-index penalty is modest — see before/after numbers."
    ),
}
print(json.dumps(verdict, indent=2))

cur.close()
conn.close()

# COMMAND ----------

dbutils.notebook.exit(json.dumps(verdict))
