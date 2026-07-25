# Databricks notebook source
# MAGIC %md
# MAGIC # T1-a — Reverse ETL: create Lakebase synced tables
# MAGIC
# MAGIC Creates 3 Lakebase **synced tables** (managed, auto-refreshed copies of gold Delta
# MAGIC tables) so the app can serve sub-10ms reads:
# MAGIC
# MAGIC | synced table | source (gold) | mode | why |
# MAGIC |---|---|---|---|
# MAGIC | `customers_synced`    | `<cat>.<sch>.customers`    | CONTINUOUS | LTV/churn/segment must show live |
# MAGIC | `transactions_synced` | `<cat>.<sch>.transactions` | CONTINUOUS | recent-activity feed |
# MAGIC | `products_synced`     | `<cat>.<sch>.products`     | TRIGGERED  | slow-changing 200-row catalog |
# MAGIC
# MAGIC Idempotent: reuses a synced table if it already exists. Run as a job (needs the
# MAGIC workspace network). Run BEFORE the staging-table notebook is not required — order
# MAGIC independent, but both must run before the app uses Lakebase.

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade databricks-sdk

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "ai_27", "Source UC catalog (gold tables)")
dbutils.widgets.text("schema", "lakebase_apps_capstone_gold", "Source schema")
dbutils.widgets.text("uc_lakebase_catalog", "ai27_lb_apps_capstone", "UC catalog backed by Lakebase")
dbutils.widgets.text("storage_catalog", "ai_27", "Catalog for sync pipeline storage")
dbutils.widgets.text("storage_schema", "pipelines", "Schema for sync pipeline storage")

CAT = dbutils.widgets.get("catalog")
SCH = dbutils.widgets.get("schema")
LB_CAT = dbutils.widgets.get("uc_lakebase_catalog")
STORAGE_CAT = dbutils.widgets.get("storage_catalog")
STORAGE_SCH = dbutils.widgets.get("storage_schema")

# Pipeline storage location must exist and be writable by us.
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {STORAGE_CAT}.{STORAGE_SCH}")
print(f"storage: {STORAGE_CAT}.{STORAGE_SCH}")

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.database import (
    SyncedDatabaseTable, SyncedTableSpec, NewPipelineSpec,
    SyncedTableSchedulingPolicy,
)

w = WorkspaceClient()


def sync(name: str, source_table: str, mode: SyncedTableSchedulingPolicy, pk: list[str]):
    """Idempotent get-or-create of a synced table in <LB_CAT>.public.<name>."""
    full_name = f"{LB_CAT}.public.{name}"
    try:
        existing = w.database.get_synced_database_table(name=full_name)
        print(f"  reusing {full_name} (status={existing.data_synchronization_status})")
        return existing
    except Exception:
        pass
    spec = SyncedTableSpec(
        source_table_full_name=source_table,
        primary_key_columns=pk,
        scheduling_policy=mode,
        new_pipeline_spec=NewPipelineSpec(
            storage_catalog=STORAGE_CAT,
            storage_schema=STORAGE_SCH,
        ),
    )
    created = w.database.create_synced_database_table(
        SyncedDatabaseTable(name=full_name, spec=spec)
    )
    print(f"  created {full_name} ({mode.value})")
    return created


sync("customers_synced",
     f"{CAT}.{SCH}.customers",
     SyncedTableSchedulingPolicy.CONTINUOUS,
     ["customer_id"])
sync("transactions_synced",
     f"{CAT}.{SCH}.transactions",
     SyncedTableSchedulingPolicy.CONTINUOUS,
     ["transaction_id"])
sync("products_synced",
     f"{CAT}.{SCH}.products",
     SyncedTableSchedulingPolicy.TRIGGERED,
     ["product_id"])

print("\nSynced tables submitted. Initial CONTINUOUS hydration can take a few minutes.")

# COMMAND ----------

import json
dbutils.notebook.exit(json.dumps({
    "CUSTOMERS_SYNCED": f"{LB_CAT}.public.customers_synced",
    "TRANSACTIONS_SYNCED": f"{LB_CAT}.public.transactions_synced",
    "PRODUCTS_SYNCED": f"{LB_CAT}.public.products_synced",
}))
