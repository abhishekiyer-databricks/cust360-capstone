"""Customer 360 — FastAPI backend.

T2 (auth): the app's two identities are wired up. Test endpoints prove each path:
- /api/whoami      → OBO: the calling user (needs the Apps proxy + consent)
- /api/whoami-sp   → the app service principal
- /api/db-check    → SELECT 1 against Lakebase via lakebase_sp() (as the SP)
- /api/config      → non-secret ids the frontend needs later

Real features (customer reads/writes, warehouse metrics, Genie, dashboard, forward-ETL)
land in T3-T7.
"""
from fastapi import FastAPI, Request

from . import config
from .auth import caller_email, obo_client, sp_client
from .db import lakebase_sp

app = FastAPI(title="Customer 360", version="0.2.0")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "customer360", "stage": "t2"}


@app.get("/api/whoami")
def whoami(request: Request):
    """OBO: identity of the calling user (401 if no proxy token — expected locally)."""
    me = obo_client(request).current_user.me()
    return {
        "identity": "obo",
        "user_name": me.user_name,
        "display_name": me.display_name,
        "email_from_header": caller_email(request),
    }


@app.get("/api/whoami-sp")
def whoami_sp():
    """Service principal: identity the app runs as for Lakebase / jobs."""
    me = sp_client().current_user.me()
    return {
        "identity": "service_principal",
        "user_name": me.user_name,
        "id": me.id,
    }


@app.get("/api/db-check")
def db_check():
    """Prove lakebase_sp() connects (as the SP, with T1-b grants) and can query."""
    with lakebase_sp() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
    return {"lakebase": "ok", "select_1": row[0] if row else None}


@app.get("/api/config")
def get_config():
    """Non-secret ids the React app needs (warehouse / dashboard / Genie)."""
    return {
        "warehouse_id": config.WAREHOUSE_ID,
        "dashboard_id": config.DASHBOARD_ID,
        "genie_space_id": config.GENIE_SPACE_ID,
    }


@app.get("/")
def root():
    return {
        "message": "Customer 360 — Databricks Apps + Lakebase capstone",
        "stage": "t2 — auth wired (OBO + SP + lakebase_sp)",
    }
