"""Auth — the app's two identities (T2).

A Databricks App runs as its own **service principal (SP)**, but users hit it as
themselves. Two identities, picked per call:

- ``obo_client(request)`` — **On-Behalf-Of the calling user.** Reads the
  ``X-Forwarded-Access-Token`` header the Apps proxy injects and builds a
  ``WorkspaceClient`` with it. Used for SQL warehouse (T3 metrics) + Genie (T5), so
  workspace RLS / permissions / audit reflect the actual user.

- ``sp_client()`` — **the app SP itself.** A bare ``WorkspaceClient()`` picks up the
  platform-injected SP creds (``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET``).
  Used for ALL Lakebase access and the forward-ETL job trigger (T7).

Hard rule (see master_plan §3-D2): Lakebase is **always** the SP — it doesn't support
OBO scopes. We attribute writes to a human via ``X-Forwarded-Email`` instead. There is
deliberately no ``lakebase_obo()``.

NOTE: OBO headers only exist behind the Apps proxy. Locally there is no
``X-Forwarded-Access-Token``, so ``obo_client`` will 401 — test OBO on the deployed app.
"""
from __future__ import annotations

from databricks.sdk import WorkspaceClient
from fastapi import HTTPException, Request

from . import config

# Header names the Databricks Apps proxy injects (case-insensitive lookup via Starlette).
_OBO_TOKEN_HEADER = "X-Forwarded-Access-Token"
_EMAIL_HEADER = "X-Forwarded-Email"


def obo_client(request: Request) -> WorkspaceClient:
    """WorkspaceClient acting as the calling user.

    Raises 401 if the OBO token is absent — we never silently fall back to the SP for
    user-facing calls (that would leak SP privileges and hide a broken OBO setup).
    """
    token = request.headers.get(_OBO_TOKEN_HEADER)
    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                "No on-behalf-of user token. This endpoint must be called through the "
                "Databricks Apps proxy with user authorization enabled and consent granted."
            ),
        )
    # auth_type="pat" pins this client to the bearer token ONLY. Without it the SDK also
    # picks up the runtime-injected SP creds (DATABRICKS_CLIENT_ID/SECRET) and errors with
    # "more than one authorization method configured: oauth and pat".
    return WorkspaceClient(host=config.DATABRICKS_HOST, token=token, auth_type="pat")


# Module-level SP client: bare WorkspaceClient() authenticates as the app SP from the
# runtime-injected creds. Stateless + reused across requests (cheap, thread-safe for our use).
# Built lazily so importing this module never fails locally when SP creds are absent.
_sp_client: WorkspaceClient | None = None


def sp_client() -> WorkspaceClient:
    """WorkspaceClient acting as the app service principal."""
    global _sp_client
    if _sp_client is None:
        _sp_client = WorkspaceClient()
    return _sp_client


def caller_email(request: Request) -> str | None:
    """Calling user's email from the proxy header — stamped into the audit log (T3)."""
    return request.headers.get(_EMAIL_HEADER)
