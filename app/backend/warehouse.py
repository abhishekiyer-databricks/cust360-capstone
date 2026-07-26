"""SQL warehouse query helper (T3B) — runs statements via the SQL Statement Execution API.

Used by the metrics + segments endpoints, which read **Delta gold via the SQL warehouse**
using the *calling user's* identity (OBO). The `WorkspaceClient` passed in is the one built
by `obo_client(request)`, so the warehouse audit log attributes the query to the real user,
and workspace RLS applies (master_plan §2 / §3-D2).

`run_query` returns rows as a list of dicts (column name -> value). Statements are
**parameterized** (`:name`) — never string-interpolated — to avoid injection and get plan
reuse (master_plan §7).
"""
from __future__ import annotations

import logging
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors.platform import PermissionDenied
from databricks.sdk.service.sql import StatementParameterListItem, StatementState
from fastapi import HTTPException

from . import config

log = logging.getLogger(__name__)

_SLOW_MS = 500  # WARNING threshold (master_plan §7 observability)


def run_query(
    ws: WorkspaceClient,
    statement: str,
    params: dict[str, object] | None = None,
    *,
    label: str = "warehouse",
) -> list[dict]:
    """Execute a parameterized SQL statement on the warehouse (as `ws`) and return dict rows.

    - `ws` — a WorkspaceClient (OBO: the calling user).
    - `statement` — SQL with `:name` parameter markers.
    - `params` — {name: value}; all bound as STRING (Databricks casts in-query as needed).
    """
    sdk_params = (
        [StatementParameterListItem(name=k, value=None if v is None else str(v)) for k, v in params.items()]
        if params
        else None
    )

    started = time.monotonic()
    try:
        resp = ws.statement_execution.execute_statement(
            statement=statement,
            warehouse_id=config.WAREHOUSE_ID,
            catalog=config.CAPSTONE_CATALOG,
            schema=config.CAPSTONE_SCHEMA,
            parameters=sdk_params,
            wait_timeout="30s",  # bounded so a slow warehouse doesn't tie up the worker
        )
    except PermissionDenied as e:
        # The OBO token reached us but lacks the `sql` scope — the user consented before the
        # scope was added to the app. Surface a clear, actionable 403 instead of a bare 500.
        msg = str(e)
        if "scopes: sql" in msg or "required scopes" in msg:
            log.warning("warehouse query %s: OBO token missing `sql` scope", label)
            raise HTTPException(
                status_code=403,
                detail=(
                    "Your session hasn't authorized warehouse (`sql`) access yet. "
                    "Re-open the app from the workspace Apps page (or clear the app's "
                    "authorization and reload) to grant the newly-requested scopes."
                ),
            ) from e
        log.warning("warehouse query %s permission denied: %s", label, msg)
        raise HTTPException(status_code=403, detail=f"Warehouse permission denied: {msg}") from e

    state = resp.status.state if resp.status else None
    if state != StatementState.SUCCEEDED:
        detail = (
            resp.status.error.message
            if resp.status and resp.status.error
            else f"statement state {state}"
        )
        log.warning("warehouse query %s failed: %s", label, detail)
        raise HTTPException(status_code=502, detail=f"Warehouse query failed: {detail}")

    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms > _SLOW_MS:
        log.warning("slow warehouse query %s took %.0fms", label, elapsed_ms)

    # Map columns -> dicts. Empty result set → [].
    result = resp.result
    if result is None or result.data_array is None:
        return []
    cols = [c.name for c in resp.manifest.schema.columns]
    return [dict(zip(cols, row)) for row in result.data_array]
