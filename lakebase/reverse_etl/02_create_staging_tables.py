# Databricks notebook source
# MAGIC %md
# MAGIC # T1-a — Reverse ETL: create Lakebase staging tables
# MAGIC
# MAGIC Creates 3 **app-owned, writable** Postgres tables in Lakebase via psycopg DDL. These
# MAGIC are where the app writes notes / segment overrides (never into synced tables — those
# MAGIC are read-only mirrors of gold). A later forward-ETL job (T7) merges them into gold.
# MAGIC
# MAGIC - `customer_notes_staging`               → forward-ETL to NEW `gold.customer_notes`
# MAGIC - `customer_segment_overrides_staging`   → forward-ETL UPDATEs `gold.customers.segment_id`
# MAGIC - `customer_audit_log`                   → append-only audit trail (stays in Lakebase)
# MAGIC
# MAGIC Idempotent (`CREATE TABLE IF NOT EXISTS`). Connects as the current user via a fresh
# MAGIC Lakebase OAuth token. Run as a job (needs the workspace network to reach the instance).

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

import uuid
import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
instance = w.database.get_database_instance(name=INSTANCE)
host = instance.read_write_dns
me = spark.sql("SELECT current_user()").collect()[0][0]
# In a job the notebook uses runtime auth (not OAuth), so w.config.oauth_token()
# is unavailable. Mint a Lakebase Postgres credential via the database API instead.
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

# COMMAND ----------

DDL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS customer_notes_staging (
    note_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id  VARCHAR(20)  NOT NULL,
    author_email VARCHAR(200) NOT NULL,
    note_text    TEXT         NOT NULL,
    sentiment    REAL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    processed    BOOLEAN      NOT NULL DEFAULT FALSE,
    processed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_notes_customer    ON customer_notes_staging (customer_id);
CREATE INDEX IF NOT EXISTS idx_notes_unprocessed ON customer_notes_staging (processed) WHERE processed = FALSE;

CREATE TABLE IF NOT EXISTS customer_segment_overrides_staging (
    override_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      VARCHAR(20)  NOT NULL UNIQUE,   -- UNIQUE → idempotent UPSERT (T3)
    override_segment VARCHAR(10)  NOT NULL,
    reason           TEXT,
    author_email     VARCHAR(200) NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    processed        BOOLEAN      NOT NULL DEFAULT FALSE,
    processed_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS customer_audit_log (
    audit_id    BIGSERIAL    PRIMARY KEY,             -- sequence → SP needs USAGE (T1-b grants)
    customer_id VARCHAR(20)  NOT NULL,
    action      VARCHAR(50)  NOT NULL,
    actor_email VARCHAR(200) NOT NULL,
    payload     JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_customer ON customer_audit_log (customer_id);
"""
cur.execute(DDL)
print("DDL applied.")

# COMMAND ----------

# Verify: list public tables (\dt equivalent)
cur.execute("""
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' ORDER BY tablename
""")
tables = [r[0] for r in cur.fetchall()]
print("public tables:")
for t in tables:
    print(f"  {t}")

cur.close()
conn.close()

# COMMAND ----------

import json
dbutils.notebook.exit(json.dumps({
    "PG_HOST": host,
    "PG_DATABASE": DB_NAME,
    "STAGING_TABLES_PRESENT": ",".join(
        t for t in tables if t in (
            "customer_notes_staging",
            "customer_segment_overrides_staging",
            "customer_audit_log",
        )
    ),
}))
