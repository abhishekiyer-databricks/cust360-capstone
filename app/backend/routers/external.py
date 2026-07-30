"""External partner API (T3a) — M2M-authenticated, gold-via-warehouse only.

Separate auth boundary from the in-app routers (master_plan §2/§3-D2, CAPSTONE_TASKS T3a):
the caller is a *service principal* that minted an OAuth bearer via the client_credentials
grant; the Apps proxy forwards it as ``X-Forwarded-Access-Token`` exactly like a human OBO
token. This handler runs the warehouse query as that SP (OBO) — it **never** touches Lakebase
and **never** falls back to the app SP, so ``system.query.history`` attributes the SELECT to
the partner SP (that's the second done-when check).

Reuses the same building blocks as the in-app metrics path: ``obo_client(request)`` (401s if
the proxy didn't forward a token, no SP fallback) + ``warehouse.run_query`` (parameterized,
PermissionDenied→403, failure→502). The only difference from the in-app ``GET /customers/{id}``
is the *source*: gold ``customers``/``transactions`` via the warehouse instead of the Lakebase
synced tables. Column names are identical (synced tables are 1:1 copies of gold), so the same
``CustomerDetail`` / ``CustomerProfile`` / ``Transaction`` models bind cleanly.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import config
from ..auth import obo_client
from ..models import CustomerDetail, CustomerProfile, Transaction
from ..warehouse import run_query

router = APIRouter(prefix="/api/external", tags=["external"])

# Profile columns mirror the in-app CustomerProfile (models.py). Explicit list, not SELECT *.
_PROFILE_COLS = (
    "customer_id, first_name, last_name, email, phone, country, city, gender, age, "
    "signup_date, last_purchase_date, segment_id, lifetime_value, churn_score, updated_at"
)


@router.get("/customers/{customer_id}", response_model=CustomerDetail)
def external_get_customer(customer_id: str, request: Request) -> CustomerDetail:
    """Same CustomerDetail shape as the in-app endpoint, but read from Delta gold via the
    SQL warehouse using the caller's (partner SP's) bearer. Never Lakebase, never the app SP.
    """
    ws = obo_client(request)  # the SP's bearer; 401 if the proxy didn't forward a token

    profile_rows = run_query(
        ws,
        f"SELECT {_PROFILE_COLS} FROM {config.gold('customers')} WHERE customer_id = :cid",
        {"cid": customer_id},
        label="external_profile",
    )
    if not profile_rows:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")

    txn_rows = run_query(
        ws,
        "SELECT transaction_id, product_id, transaction_date, channel, status, amount "
        f"FROM {config.gold('transactions')} WHERE customer_id = :cid "
        "ORDER BY transaction_date DESC NULLS LAST, transaction_id DESC LIMIT 20",
        {"cid": customer_id},
        label="external_txns",
    )

    return CustomerDetail(
        profile=CustomerProfile(**profile_rows[0]),
        recent_transactions=[Transaction(**r) for r in txn_rows],
    )
