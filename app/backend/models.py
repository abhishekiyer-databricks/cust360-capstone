"""Pydantic response/request models (T3).

Why these exist (master_plan §7 API hygiene):
- **Typed contract** — FastAPI validates outgoing payloads against these and documents them
  in OpenAPI, so the React client's TypeScript types have a single source of truth.
- **Minimal payloads** — the list row (`Customer`) carries only the ~handful of columns the
  table renders, NOT `SELECT *`. The detail view carries more.

Field names mirror the gold column names (the Lakebase synced tables are 1:1 copies of gold,
see CAPSTONE_TASKS.md "Provisioned gold tables" + t1_plan).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Pagination envelope (D1). Generic so we can reuse it for any list endpoint.
# ---------------------------------------------------------------------------
T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Server-side pagination envelope: {items, total, page, page_size}."""

    items: list[T]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Customer list row — minimal set the table needs (NOT SELECT *).
# ---------------------------------------------------------------------------
class Customer(BaseModel):
    customer_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    country: str | None = None
    segment_id: str | None = None
    lifetime_value: float | None = None
    churn_score: float | None = None


# ---------------------------------------------------------------------------
# Customer detail — profile fields + recent activity.
# ---------------------------------------------------------------------------
class Transaction(BaseModel):
    transaction_id: str
    product_id: str | None = None
    transaction_date: date | None = None
    channel: str | None = None
    status: str | None = None
    amount: float | None = None


class CustomerProfile(BaseModel):
    """Full profile shown on the Profile tab."""

    customer_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    country: str | None = None
    city: str | None = None
    gender: str | None = None
    age: int | None = None
    signup_date: date | None = None
    last_purchase_date: date | None = None
    segment_id: str | None = None
    lifetime_value: float | None = None
    churn_score: float | None = None
    updated_at: datetime | None = None


class CustomerDetail(BaseModel):
    """Profile + last-20 transactions (the two read-path queries on the detail page)."""

    profile: CustomerProfile
    recent_transactions: list[Transaction]


# ---------------------------------------------------------------------------
# Metrics (T3B) — cross-table aggregate computed on gold via the SQL warehouse (OBO).
# ---------------------------------------------------------------------------
class CategorySpend(BaseModel):
    category: str | None = None
    amount: float


class CustomerMetrics(BaseModel):
    """Live aggregate across transactions × products × support_tickets (gold)."""

    lifetime_spend: float
    spend_30d: float
    spend_90d: float
    top_categories: list[CategorySpend]
    open_tickets: int
    avg_csat: float | None = None


# ---------------------------------------------------------------------------
# Segments (T3B) — the 7-8 named segments for the filter dropdown + display.
# ---------------------------------------------------------------------------
class Segment(BaseModel):
    segment_id: str
    segment_name: str | None = None


# ---------------------------------------------------------------------------
# Writes (T3C) — notes + segment overrides land in Lakebase staging (app SP), each paired
# with an audit row in the same transaction. Actor email comes from X-Forwarded-Email.
# ---------------------------------------------------------------------------
class NoteCreate(BaseModel):
    note_text: str = Field(min_length=1, max_length=5000)


class Note(BaseModel):
    note_id: str
    customer_id: str
    author_email: str
    note_text: str
    created_at: datetime


class SegmentOverrideCreate(BaseModel):
    override_segment: str = Field(min_length=1, max_length=10)
    reason: str | None = Field(default=None, max_length=2000)


class SegmentOverrideResult(BaseModel):
    customer_id: str
    override_segment: str
    changed: bool  # False when re-submitting the same value (idempotent no-op)


# ---------------------------------------------------------------------------
# Forward-ETL job (T7) — the Reports page triggers the job (as the app SP) and polls status.
# These trim the raw Jobs SDK objects down to what the UI needs.
# ---------------------------------------------------------------------------
class JobRunTriggered(BaseModel):
    """Returned by POST /run-forward-etl — enough to start polling."""

    run_id: int
    run_page_url: str | None = None


class JobRun(BaseModel):
    """A single job run, trimmed for the status indicator + recent-runs table."""

    run_id: int
    # life_cycle_state: PENDING / RUNNING / TERMINATING / TERMINATED / SKIPPED / INTERNAL_ERROR
    life_cycle_state: str | None = None
    # result_state (only once terminal): SUCCESS / FAILED / TIMEDOUT / CANCELED
    result_state: str | None = None
    start_time: int | None = None  # epoch ms (as the Jobs API returns)
    end_time: int | None = None
    run_page_url: str | None = None
