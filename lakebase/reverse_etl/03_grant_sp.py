# Databricks notebook source
# MAGIC %md
# MAGIC # T1-b — Grant the app service principal on Lakebase
# MAGIC
# MAGIC Fresh Postgres roles have **zero** privileges. The app connects to Lakebase as a PG
# MAGIC role whose **name is the app SP's `client_id` UUID**. Until we grant it, every app
# MAGIC query fails with "permission denied". This one-time step grants read on synced tables,
# MAGIC read/write on staging, USAGE on sequences, and sets ALTER DEFAULT PRIVILEGES so future
# MAGIC synced tables inherit SELECT.
# MAGIC
# MAGIC **Run this AFTER** the app is first deployed (T8-min) — the SP's PG role only exists
# MAGIC once the SP has logged into Lakebase at least once. If the role doesn't exist yet,
# MAGIC this notebook prints how to trigger a login and exits without error.
# MAGIC
# MAGIC Pass the SP `client_id` (UUID) from `databricks apps get <app> -o json`
# MAGIC (`service_principal_client_id`) as the `sp_client_id` widget.

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade databricks-sdk psycopg2-binary

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("instance_name", "ai27-lb-apps-capstone", "Lakebase instance name")
dbutils.widgets.text("database_name", "cust360ai27", "Postgres database name")
dbutils.widgets.text("sp_client_id", "", "App SP client_id (UUID) = PG role name")

INSTANCE = dbutils.widgets.get("instance_name")
DB_NAME = dbutils.widgets.get("database_name")
SP_ROLE = dbutils.widgets.get("sp_client_id").strip()

assert SP_ROLE, "sp_client_id is required (the app SP's client_id UUID)."

# COMMAND ----------

import uuid
import psycopg2
from psycopg2 import sql
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
instance = w.database.get_database_instance(name=INSTANCE)
host = instance.read_write_dns
me = spark.sql("SELECT current_user()").collect()[0][0]
# Job runtime auth isn't OAuth, so mint a Lakebase credential via the database API.
token = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()), instance_names=[INSTANCE]
).token

conn = psycopg2.connect(
    host=host, port=5432, dbname=DB_NAME,
    user=me, password=token, sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()

# COMMAND ----------

# Confirm the SP role exists (created lazily on the SP's first Lakebase login).
cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (SP_ROLE,))
if cur.fetchone() is None:
    print(f"⚠️  Role '{SP_ROLE}' does not exist yet.")
    print("    The app SP must log into Lakebase once before it can be granted.")
    print("    Fix: hit an app endpoint that touches Lakebase (or run a SELECT 1 as the SP),")
    print("    then re-run this notebook.")
    cur.close(); conn.close()
    dbutils.notebook.exit("ROLE_MISSING")

print(f"Role '{SP_ROLE}' exists — applying grants.")

# COMMAND ----------

role = sql.Identifier(SP_ROLE)
stmts = [
    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role),
    sql.SQL(
        "GRANT SELECT ON customers_synced, transactions_synced, products_synced TO {}"
    ).format(role),
    sql.SQL(
        "GRANT SELECT, INSERT, UPDATE ON customer_notes_staging, "
        "customer_segment_overrides_staging, customer_audit_log TO {}"
    ).format(role),
    sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role),
    sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}"
    ).format(role),
]
for s in stmts:
    cur.execute(s)
    print(f"  ok: {s.as_string(conn)}")

# COMMAND ----------

# Show what the SP role can now touch.
cur.execute(
    """
    SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type)
    FROM information_schema.role_table_grants
    WHERE grantee = %s AND table_schema = 'public'
    GROUP BY table_name ORDER BY table_name
    """,
    (SP_ROLE,),
)
print(f"grants for {SP_ROLE}:")
for tbl, privs in cur.fetchall():
    print(f"  {tbl}: {privs}")

cur.close()
conn.close()

# COMMAND ----------

dbutils.notebook.exit("GRANTED")
