"""Central config — one place that reads environment variables.

On the Databricks Apps runtime these come from `app.yaml`'s `env:` block plus the
platform-injected service-principal creds (`DATABRICKS_CLIENT_ID`,
`DATABRICKS_CLIENT_SECRET`, `DATABRICKS_HOST`). Locally they come from `app/.env`
(loaded here via python-dotenv) so smoke tests can import the module without crashing.

Nothing here is a secret: only ids/hostnames. Secret-backed values (`valueFrom`) are a
T6 concern.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# Local dev convenience: load app/.env if present. No-op on the Apps runtime (env is
# already populated by the platform + app.yaml), and never overrides real env vars.
load_dotenv(override=False)


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


# Workspace / SDK
DATABRICKS_HOST: str | None = _get("DATABRICKS_HOST")

# App service principal. The runtime injects DATABRICKS_CLIENT_ID = the app SP's
# client_id, which is ALSO the Lakebase Postgres role name granted in T1-b. Allow an
# explicit SP_CLIENT_ID override for local testing, otherwise fall back to the injected id.
SP_CLIENT_ID: str | None = _get("SP_CLIENT_ID") or _get("DATABRICKS_CLIENT_ID")

# Lakebase (Postgres)
PGHOST: str | None = _get("PGHOST")
PGDATABASE: str | None = _get("PGDATABASE")
PG_INSTANCE_NAME: str | None = _get("PG_INSTANCE_NAME")
PGPORT: int = int(_get("PGPORT", "5432") or "5432")

# Data-service ids the frontend needs (exposed via /api/config; non-secret)
WAREHOUSE_ID: str | None = _get("WAREHOUSE_ID")
DASHBOARD_ID: str | None = _get("DASHBOARD_ID")
GENIE_SPACE_ID: str | None = _get("GENIE_SPACE_ID")

# Forward-ETL job (T7). The app SP triggers this job by id (jobs.run_now). Set as a plain
# env value after the job's first `bundle deploy` creates it; T6 upgrades this to a
# `valueFrom` binding of the jobs resource in resources/app.yml.
FORWARD_ETL_JOB_ID: str | None = _get("FORWARD_ETL_JOB_ID")

# Gold Delta catalog/schema — the SQL-warehouse (OBO) read path for metrics + segments (T3B).
# The synced tables are the fast SP read path; these gold tables are queried via the warehouse.
CAPSTONE_CATALOG: str | None = _get("CAPSTONE_CATALOG")
CAPSTONE_SCHEMA: str | None = _get("CAPSTONE_SCHEMA")


def gold(table: str) -> str:
    """Fully-qualified gold table name, e.g. gold('customers') -> ai_27.<schema>.customers."""
    return f"{CAPSTONE_CATALOG}.{CAPSTONE_SCHEMA}.{table}"
