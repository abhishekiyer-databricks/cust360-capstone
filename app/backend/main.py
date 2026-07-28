"""Customer 360 — FastAPI backend.

Stages so far:
- T2 (auth): the app's two identities are wired. Test endpoints prove each path:
  - /api/whoami      → OBO: the calling user (needs the Apps proxy + consent)
  - /api/whoami-sp   → the app service principal
  - /api/db-check    → SELECT 1 against Lakebase via lakebase_sp() (as the SP)
  - /api/config      → non-secret ids the frontend needs
- T3 slice 3A (reads): customer list + detail from Lakebase synced tables (app SP), plus the
  React SPA is served from backend/static. See routers/customers.py.

Metrics (warehouse+OBO), writes (staging+audit), Genie, dashboard, forward-ETL land in
later slices/tasks.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import config
from .auth import caller_email, obo_client, sp_client
from .db import lakebase_sp
from .routers import customers, jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Customer 360", version="0.3.0")

# Compress responses > 1KB (master_plan §7 API hygiene).
app.add_middleware(GZipMiddleware, minimum_size=1000)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach/echo an X-Request-Id so a request can be correlated React → FastAPI → Lakebase.

    Generates one if the client didn't send it; echoes it back on the response.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


app.add_middleware(RequestIdMiddleware)

# ---------------------------------------------------------------------------
# API routers
# ---------------------------------------------------------------------------
app.include_router(customers.router)
app.include_router(jobs.router)


# ---------------------------------------------------------------------------
# Health + auth test endpoints (T2)
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "service": "customer360", "stage": "t7-7b"}


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
    return {"identity": "service_principal", "user_name": me.user_name, "id": me.id}


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


# ---------------------------------------------------------------------------
# Serve the built React SPA (must be mounted AFTER all /api routes).
#
# The frontend is built locally (`npm run build` → backend/static, see vite.config.ts) and
# uploaded with the app (master_plan D5/D6). If the bundle isn't present yet (e.g. backend
# started before the first frontend build), we skip the mount so the API still boots.
# `html=True` serves index.html at "/"; a catch-all returns index.html for client-side routes
# (e.g. /customers/C0003600) so a full-page refresh on a deep link doesn't 404.
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent / "static"

if (_STATIC_DIR / "index.html").exists():
    # Real static files (JS/CSS/assets) served from /assets etc.
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")

    @app.get("/")
    def spa_root():
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/{full_path:path}")
    def spa_catch_all(full_path: str):
        """Return index.html for any non-API path so React Router handles the route.

        (API routes are already registered above and take precedence; a request for a real
        static asset that doesn't exist will fall through to index.html, which is fine for an
        SPA.)
        """
        candidate = _STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_STATIC_DIR / "index.html")
else:
    log.warning("No built frontend at %s — serving API only. Run `npm run build`.", _STATIC_DIR)

    @app.get("/")
    def root():
        return {
            "message": "Customer 360 — API only (frontend not built yet)",
            "stage": "t3-3a",
        }
