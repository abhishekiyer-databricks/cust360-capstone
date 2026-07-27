# Databricks notebook source
# MAGIC %md
# MAGIC # T7 — Forward ETL (Pattern A): Lakebase staging → Delta gold
# MAGIC
# MAGIC Promotes the notes / segment-overrides the app writes into Lakebase **staging** back
# MAGIC into **Delta gold**, on demand. Triggered by the app's "Run forward-ETL" button (Reports
# MAGIC page → Jobs API, as the app SP), or manually via `databricks bundle run forward_etl`.
# MAGIC
# MAGIC **Pattern A** (master_plan §3-D3): read unprocessed staging rows over psycopg → build a
# MAGIC Spark DataFrame → `MERGE INTO` gold → mark those rows `processed=true`.
# MAGIC
# MAGIC | staging source | → gold destination | how |
# MAGIC |---|---|---|
# MAGIC | `customer_notes_staging` | **NEW** `gold.customer_notes` | MERGE on `note_id` (INSERT-only; notes are immutable) |
# MAGIC | `customer_segment_overrides_staging` | `gold.customers.segment_id` | MERGE on `customer_id` (UPDATE) |
# MAGIC | `customer_audit_log` | *(never promoted — stays in Lakebase)* | — |
# MAGIC
# MAGIC ### Idempotency (the key correctness point — see t7_plan §1.2)
# MAGIC The MERGE (Delta/Spark) and the `processed` flag update (Postgres/Lakebase) are in **two
# MAGIC different systems**, so there is **no single cross-system transaction**. Instead:
# MAGIC 1. read unprocessed rows and **capture their ids**,
# MAGIC 2. `MERGE INTO` gold keyed on the business key (re-merging a row is a no-op), THEN
# MAGIC 3. `UPDATE ... SET processed=true` **only for the captured ids**, committed in Postgres.
# MAGIC
# MAGIC If the job dies between (2) and (3), the rows stay `processed=false` and the next run
# MAGIC re-merges them harmlessly (MERGE is idempotent on the key) — we never lose or duplicate
# MAGIC data. Order matters: **merge first, mark second.**
# MAGIC
# MAGIC `gold.customer_notes` is created here (`CREATE TABLE IF NOT EXISTS`) so this task is
# MAGIC self-contained. Runs on serverless job compute; connects to Lakebase as the current user
# MAGIC (the deployer, who owns gold + staging in dev — no extra grants needed for T7).

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade databricks-sdk psycopg2-binary

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("instance_name", "ai27-lb-apps-capstone", "Lakebase instance name")
dbutils.widgets.text("database_name", "cust360ai27", "Postgres database name")
dbutils.widgets.text("catalog", "ai_27", "Gold UC catalog")
dbutils.widgets.text("schema", "lakebase_apps_capstone_gold", "Gold schema")

INSTANCE = dbutils.widgets.get("instance_name")
DB_NAME = dbutils.widgets.get("database_name")
CAT = dbutils.widgets.get("catalog")
SCH = dbutils.widgets.get("schema")

NOTES_GOLD = f"{CAT}.{SCH}.customer_notes"
CUSTOMERS_GOLD = f"{CAT}.{SCH}.customers"

# COMMAND ----------

import uuid
import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
instance = w.database.get_database_instance(name=INSTANCE)
host = instance.read_write_dns
me = spark.sql("SELECT current_user()").collect()[0][0]
# Job runtime auth isn't OAuth, so mint a Lakebase Postgres credential via the database API
# (same pattern as the reverse_etl notebooks; see reverse_etl gotcha #1/#4).
token = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()), instance_names=[INSTANCE]
).token

print(f"connecting to {host}/{DB_NAME} as {me}")
conn = psycopg2.connect(
    host=host, port=5432, dbname=DB_NAME,
    user=me, password=token, sslmode="require",
)
# Autocommit OFF: we mark rows processed in an explicit Postgres transaction after the MERGE.
conn.autocommit = False

# COMMAND ----------

# MAGIC %md
# MAGIC ## Destination table (idempotent create)
# MAGIC `gold.customer_notes` is new (the write-path destination). `gold.customers` already
# MAGIC exists (provisioned gold table) — the override MERGE just UPDATEs its `segment_id`.

# COMMAND ----------

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {NOTES_GOLD} (
        note_id      STRING,
        customer_id  STRING,
        author_email STRING,
        note_text    STRING,
        sentiment    FLOAT,
        created_at   TIMESTAMP,
        merged_at    TIMESTAMP
    ) USING DELTA
""")
print(f"ensured {NOTES_GOLD}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1) Notes: staging → `gold.customer_notes` (MERGE on note_id, INSERT-only)

# COMMAND ----------

from datetime import datetime, timezone
from pyspark.sql.types import (
    StructType, StructField, StringType, FloatType, TimestampType,
)

NOTES_SCHEMA = StructType([
    StructField("note_id", StringType(), False),
    StructField("customer_id", StringType(), False),
    StructField("author_email", StringType(), True),
    StructField("note_text", StringType(), True),
    StructField("sentiment", FloatType(), True),
    StructField("created_at", TimestampType(), True),
])

notes_merged = 0
with conn.cursor() as cur:
    cur.execute("""
        SELECT note_id, customer_id, author_email, note_text, sentiment, created_at
        FROM customer_notes_staging
        WHERE processed = FALSE
    """)
    note_rows = cur.fetchall()

if note_rows:
    note_ids = [str(r[0]) for r in note_rows]
    # Normalise psycopg types for Spark: UUID -> str, keep datetime/float/None as-is.
    data = [
        (str(r[0]), r[1], r[2], r[3], (float(r[4]) if r[4] is not None else None), r[5])
        for r in note_rows
    ]
    staged_notes = spark.createDataFrame(data, schema=NOTES_SCHEMA)
    staged_notes.createOrReplaceTempView("staged_notes")

    spark.sql(f"""
        MERGE INTO {NOTES_GOLD} t
        USING staged_notes s
          ON t.note_id = s.note_id
        WHEN NOT MATCHED THEN INSERT (
            note_id, customer_id, author_email, note_text, sentiment, created_at, merged_at
        ) VALUES (
            s.note_id, s.customer_id, s.author_email, s.note_text, s.sentiment,
            s.created_at, current_timestamp()
        )
    """)
    notes_merged = len(note_ids)
    print(f"merged {notes_merged} note(s) into {NOTES_GOLD}")
else:
    note_ids = []
    print("no unprocessed notes")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2) Overrides: staging → `gold.customers.segment_id` (MERGE on customer_id, UPDATE)

# COMMAND ----------

OVERRIDES_SCHEMA = StructType([
    StructField("customer_id", StringType(), False),
    StructField("override_segment", StringType(), False),
])

overrides_merged = 0
with conn.cursor() as cur:
    cur.execute("""
        SELECT override_id, customer_id, override_segment
        FROM customer_segment_overrides_staging
        WHERE processed = FALSE
    """)
    ovr_rows = cur.fetchall()

if ovr_rows:
    override_ids = [str(r[0]) for r in ovr_rows]
    data = [(r[1], r[2]) for r in ovr_rows]
    staged_overrides = spark.createDataFrame(data, schema=OVERRIDES_SCHEMA)
    staged_overrides.createOrReplaceTempView("staged_overrides")

    spark.sql(f"""
        MERGE INTO {CUSTOMERS_GOLD} t
        USING staged_overrides s
          ON t.customer_id = s.customer_id
        WHEN MATCHED THEN UPDATE SET
            t.segment_id = s.override_segment,
            t.updated_at = current_timestamp()
    """)
    overrides_merged = len(override_ids)
    print(f"merged {overrides_merged} override(s) into {CUSTOMERS_GOLD}.segment_id")
else:
    override_ids = []
    print("no unprocessed overrides")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3) Mark processed (Postgres txn — AFTER the MERGEs succeed)
# MAGIC Only the ids captured before the MERGE are flagged, so rows the app inserted *during*
# MAGIC this run stay `processed=false` for the next run.

# COMMAND ----------

with conn.cursor() as cur:
    # note_id / override_id are Postgres UUID columns, but we hold them as Python strings
    # (converted for Spark). Cast the param array to uuid[] so `= ANY(...)` type-matches —
    # otherwise Postgres errors "operator does not exist: uuid = text".
    if note_ids:
        cur.execute(
            "UPDATE customer_notes_staging "
            "SET processed = TRUE, processed_at = NOW() "
            "WHERE note_id = ANY(%s::uuid[])",
            (note_ids,),
        )
    if override_ids:
        cur.execute(
            "UPDATE customer_segment_overrides_staging "
            "SET processed = TRUE, processed_at = NOW() "
            "WHERE override_id = ANY(%s::uuid[])",
            (override_ids,),
        )
conn.commit()
print(f"marked processed: {len(note_ids)} note(s), {len(override_ids)} override(s)")

cur = conn.cursor()
cur.close()
conn.close()

# COMMAND ----------

import json
result = {
    "notes_merged": notes_merged,
    "overrides_merged": overrides_merged,
    "gold_notes_table": NOTES_GOLD,
}
print(result)
dbutils.notebook.exit(json.dumps(result))
