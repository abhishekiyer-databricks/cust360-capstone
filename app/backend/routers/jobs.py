"""Forward-ETL job endpoints (T7, slice 7B) — trigger + poll + history, as the app SP.

The Reports page uses these to run the forward-ETL job (Lakebase staging → Delta gold, the
Pattern A notebook from 7A) and watch it complete:

- POST /api/jobs/run-forward-etl  — trigger a run  (jobs.run_now)
- GET  /api/jobs/{run_id}         — poll one run's status (jobs.get_run)
- GET  /api/jobs/runs             — recent runs for the history table (jobs.list_runs)

All three run as the **app service principal** (`sp_client()`), NOT OBO: triggering jobs is
app-level work, not tied to a user (master_plan §3-D2). The SP has CAN_MANAGE_RUN on the job
(granted declaratively in resources/jobs.yml). No X-Forwarded-* headers are needed here.

The job id comes from `config.FORWARD_ETL_JOB_ID` (set in app.yaml after 7A's deploy; T6
upgrades it to a valueFrom binding). If it's unset we return 503 — a clear "not configured"
signal rather than an opaque 500.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from .. import config
from ..auth import sp_client
from ..models import JobRun, JobRunTriggered

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job_id() -> int:
    """The forward-ETL job id, or 503 if it isn't configured yet."""
    if not config.FORWARD_ETL_JOB_ID:
        raise HTTPException(
            status_code=503,
            detail=(
                "Forward-ETL job id is not configured (FORWARD_ETL_JOB_ID). Deploy the "
                "`forward_etl` job and set it in app.yaml."
            ),
        )
    try:
        return int(config.FORWARD_ETL_JOB_ID)
    except ValueError:
        raise HTTPException(status_code=503, detail="FORWARD_ETL_JOB_ID is not a valid job id.")


def _to_job_run(run) -> JobRun:
    """Trim a raw Jobs SDK run object to the fields the UI needs."""
    state = run.state
    return JobRun(
        run_id=run.run_id,
        life_cycle_state=(
            state.life_cycle_state.value if state and state.life_cycle_state else None
        ),
        result_state=(state.result_state.value if state and state.result_state else None),
        start_time=run.start_time or None,
        end_time=run.end_time or None,
        run_page_url=run.run_page_url,
    )


@router.post("/run-forward-etl", response_model=JobRunTriggered)
def run_forward_etl():
    """Trigger the forward-ETL job as the app SP; return the new run id to poll."""
    job_id = _job_id()
    try:
        waiter = sp_client().jobs.run_now(job_id=job_id)
    except Exception as exc:  # SDK raises for permission / not-found / transport errors
        log.exception("run_now failed for job %s", job_id)
        raise HTTPException(status_code=502, detail=f"Could not trigger the job: {exc}")

    run_id = waiter.run_id
    log.info("Triggered forward-ETL job %s → run %s", job_id, run_id)
    # Fetch the run once so we can hand back a deep link to the run page.
    try:
        run = sp_client().jobs.get_run(run_id=run_id)
        page_url = run.run_page_url
    except Exception:
        page_url = None
    return JobRunTriggered(run_id=run_id, run_page_url=page_url)


@router.get("/runs", response_model=list[JobRun])
def list_forward_etl_runs(limit: int = 10):
    """Recent runs of the forward-ETL job (for the Reports history table)."""
    job_id = _job_id()
    try:
        runs = sp_client().jobs.list_runs(job_id=job_id, limit=min(max(limit, 1), 25))
    except Exception as exc:
        log.exception("list_runs failed for job %s", job_id)
        raise HTTPException(status_code=502, detail=f"Could not list runs: {exc}")
    return [_to_job_run(r) for r in runs]


@router.get("/{run_id}", response_model=JobRun)
def get_forward_etl_run(run_id: int):
    """Poll a single run's status (the Reports page calls this on an interval)."""
    try:
        run = sp_client().jobs.get_run(run_id=run_id)
    except Exception as exc:
        log.exception("get_run failed for run %s", run_id)
        raise HTTPException(status_code=502, detail=f"Could not get run status: {exc}")
    return _to_job_run(run)
