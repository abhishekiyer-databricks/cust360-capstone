"""Customer 360 — FastAPI backend.

T8-min: minimal deployable app. Real features (auth, Lakebase reads, warehouse,
Genie, dashboard, forward-ETL) are added in T2-T7. For now this proves the
git-source DABs deploy path and creates the app service principal.
"""
from fastapi import FastAPI

app = FastAPI(title="Customer 360", version="0.1.0")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "customer360", "stage": "t8-min"}


@app.get("/")
def root():
    return {
        "message": "Customer 360 — Databricks Apps + Lakebase capstone",
        "stage": "t8-min hello world; features land in T2-T7",
    }
