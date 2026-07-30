# T3a — External API: partner access via M2M

> **Status:** planned (next task after T8). Style matches `t3_plan.md` / `t7_plan.md`:
> concept → design decisions → step-by-step → deploy/test → done-when.
> **Prereq context:** T2 (OBO + SP), T3B (warehouse OBO read path), T8 (git-source app) all
> DONE. This task adds a *second, separate auth boundary* on top of infrastructure that
> already exists — it is mostly configuration + a thin router + two test scripts.

---

## 0. Where this sits

| | In-app endpoints (T3) | **External endpoint (T3a)** |
|---|---|---|
| Caller | A human in a browser | A **partner service principal** (a machine) |
| How the token is minted | Browser SSO → Apps proxy injects OBO token | SP runs OAuth **client_credentials** grant against `/oidc/v1/token` |
| What reaches the handler | `X-Forwarded-Access-Token` (user's identity) | `X-Forwarded-Access-Token` (**SP's** identity) |
| Data path | Lakebase synced tables (SP) + warehouse (OBO) | **gold via warehouse only** (OBO = the SP's bearer) |
| Lakebase? | Yes (list/detail/writes) | **Never.** No SP-role fallback, no synced tables. |

The key realization (and the reason this is a *separate* task, not just another route):
**the Apps proxy treats a machine caller exactly like a human caller.** Both authenticate
to the proxy, the proxy strips `Authorization` and forwards `X-Forwarded-Access-Token`. So
the handler code is nearly identical to the T3B metrics path — the difference is entirely in
**who mints the token and what grants they hold**, not in the request handling.

---

## 1. Concept — what this teaches

1. **M2M (machine-to-machine) OAuth.** A partner has no browser and no interactive login. It
   holds an SP **client_id + client_secret** and performs the OAuth 2.0 *client_credentials*
   grant: POST those creds to `https://<host>/oidc/v1/token`, get back a short-lived OAuth
   **access_token**. That access_token — NOT the client_secret — is the Bearer. The Databricks
   SDK does this grant for us automatically when configured with the SP creds.

2. **Two independent permission layers.** A valid OAuth bearer is necessary but not
   sufficient. The SP needs:
   - **CAN_USE on the app** — or the *Apps proxy* rejects it with 401 before your code runs.
   - **Warehouse CAN_USE + gold SELECT** — or your code runs but the *warehouse query* fails
     with INSUFFICIENT_PERMISSIONS.
   These are checked at two different gates (proxy vs. warehouse), which is why a
   "403 from the warehouse" and a "401 from the proxy" mean very different things.

3. **Audit attribution.** Because the handler uses the caller's bearer (OBO) to run the
   warehouse statement, `system.query.history` attributes the SELECT to the **partner SP**,
   not to the deploying user or the app SP. That's the second done-when check and the whole
   point of the OBO-not-SP-fallback rule here.

---

## 2. Design decisions

### DA1 — Which service principal is the "partner"?
The task allows either the app's own SP or a separate "partner integration" SP.
**Decision: create a DEDICATED partner SP** (display name `cust360-partner`) rather than
reuse the app's own SP.
- **Why:** this is the *realistic* test and it makes the security boundary honest. The whole
  point of T3a is a **separate auth boundary** — a partner is not the app. Reusing the app SP
  muddies that: the app SP can CAN_MANAGE the app and touch Lakebase, so "attributed to the
  SP" in the audit log wouldn't cleanly prove "a partner, distinct from the app." A dedicated
  SP holding **only** CAN_USE + gold SELECT proves least-privilege for real, and the done-when
  audit check (statement attributed to the SP, not the deploying user) is far more convincing.
- **Cost:** ~5-10 min of one-time setup — only two genuinely new steps (create SP + mint its
  OAuth secret); the CAN_USE + gold grants would be needed for *any* SP. Worth it.
- **Bonus (teaches the gates):** a fresh SP starts with **zero** access, so each missing grant
  actually fails — a cleaner live demonstration of the two permission gates (proxy CAN_USE vs.
  warehouse/UC SELECT) than reusing an SP that already reads gold.
- **Grant caveat:** the task warns *"CAN_USE — CAN_MANAGE doesn't replace it explicitly on
  some workspaces."* With a dedicated partner SP we grant CAN_USE explicitly regardless (it's
  the only app permission it should ever have).
- **Fallback:** if SP creation is blocked (permissions), reuse the app SP
  (`client_id 10c64b22-ac46-4123-bb60-041dc9d4fa92`, `sp_id 144899163311454`) and note in the
  writeup that production would use a dedicated least-privilege SP. The M2M *flow* is identical
  either way — only the identity + grant target change.

### DA2 — Reuse the existing warehouse helper, don't fork it
`app/backend/warehouse.py::run_query(ws, sql, params)` already does *exactly* the right
thing: takes a `WorkspaceClient`, runs a parameterized statement on `WAREHOUSE_ID` against
`CAPSTONE_CATALOG.CAPSTONE_SCHEMA`, maps rows→dicts, handles PermissionDenied→403 and
failures→502, logs slow queries. The external handler will call it with the OBO client, same
as the metrics endpoint. **No new query engine, no new error handling.** This keeps the
external surface honestly "the same data path as in-app metrics, different caller."

### DA3 — Reuse the `CustomerDetail` response model
The task says *"Returns the same `CustomerDetail` shape as the in-app endpoint."* We already
have `CustomerDetail = {profile: CustomerProfile, recent_transactions: [Transaction]}` in
`models.py`. **Reuse it verbatim.** The only difference from the in-app `GET /customers/{id}`
is the *source*: gold `customers` / `transactions` via the warehouse, instead of the Lakebase
synced tables. Column names are identical (synced tables are 1:1 copies of gold), so the same
Pydantic models bind cleanly.

### DA4 — Router prefix + no shared dependencies with in-app routers
New file `app/backend/routers/external.py`, mounted at prefix **`/api/external`**. It imports
`obo_client` and `warehouse.run_query` but nothing Lakebase. This makes the separation
visible in the code tree (the task explicitly asks for "a new path prefix so it's clearly
separate from in-app routers").

### DA5 — `obo_client` is reused as-is (it already 401s without a token)
The existing `auth.obo_client(request)` reads `X-Forwarded-Access-Token`, 401s if absent, and
pins `auth_type="pat"`. That is precisely the external contract: *"Handler reads
`X-Forwarded-Access-Token`, builds a `WorkspaceClient(token=…)`, never falls back to the app
SP."* No new auth helper needed. (There is deliberately **no** SP fallback — that rule is
already enforced by `obo_client`.)

### DA6 — Test scripts run from the *developer's* machine, hitting the *deployed* app
`examples/m2m_test.py` is not a unit test — it exercises the real M2M flow end-to-end against
the live app URL. It reads creds from env, uses the SDK to mint the bearer, and calls the
deployed `/api/external/customers/{id}`. It cannot meaningfully run against a local uvicorn
(no Apps proxy → no `X-Forwarded-Access-Token` injection → the handler would 401). **This is
a deployed-app test, like every OBO test in this project.**

---

## 3. Files touched

```
app/backend/routers/external.py    NEW — GET /api/external/customers/{id}
app/backend/main.py                EDIT — include_router(external.router); bump stage tag
examples/_token.py                 NEW — SDK M2M helper → returns OAuth bearer
examples/m2m_test.py               NEW — happy-path E2E test, prints 200 + JSON
examples/README.md                 NEW (optional) — how to set env + run
```
No changes to `models.py` (reuse `CustomerDetail`), `warehouse.py`, `config.py`, `app.yaml`,
or `resources/*`. The only non-code work is **grants** (steps in §5), applied via CLI/UI, not
committed as bundle config (they're on an SP identity, not a bundle resource — keeping them
out of `resources/` avoids coupling the demo SP into the deploy).

---

## 4. Step-by-step implementation

### 4.1 `app/backend/routers/external.py`
Mirror the in-app detail query but against gold via the warehouse:

```python
"""External partner API (T3a) — M2M-authenticated, gold-via-warehouse only.

Separate auth boundary from the in-app routers (master_plan §2/§3-D2, CAPSTONE_TASKS T3a):
the caller is a *service principal* that minted an OAuth bearer via the client_credentials
grant; the Apps proxy forwards it as X-Forwarded-Access-Token exactly like a human OBO token.
This handler runs the warehouse query as that SP (OBO) — it NEVER touches Lakebase and NEVER
falls back to the app SP, so system.query.history attributes the SELECT to the partner SP.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import config
from ..auth import obo_client
from ..models import CustomerDetail, CustomerProfile, Transaction
from ..warehouse import run_query

router = APIRouter(prefix="/api/external", tags=["external"])

_PROFILE_COLS = (
    "customer_id, first_name, last_name, email, phone, country, city, gender, age, "
    "signup_date, last_purchase_date, segment_id, lifetime_value, churn_score, updated_at"
)


@router.get("/customers/{customer_id}", response_model=CustomerDetail)
def external_get_customer(customer_id: str, request: Request) -> CustomerDetail:
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
```

Notes:
- `run_query` binds params as STRING; Databricks casts `age`/`lifetime_value`/etc. Pydantic
  then coerces the string cells back to the model's `int|float|date` types on the way out
  (same as the metrics path). Confirm the date/decimal cells deserialize cleanly during the
  test — if the warehouse returns numbers as strings, Pydantic v2 coerces them, but verify.
- `:cid` named parameter (warehouse uses `:name`), unlike the Lakebase path's `%s`.

### 4.2 `app/backend/main.py`
```python
from .routers import customers, external, genie, jobs
...
app.include_router(external.router)
```
Bump the health `stage` tag to `"t3a-external"`.

### 4.3 `examples/_token.py`
Shared M2M helper — let the SDK do the client_credentials grant:

```python
"""M2M helper: run the OAuth client_credentials grant and return an OAuth *access_token*.

The partner never sends its client_secret as the Bearer — it exchanges (client_id +
client_secret) at /oidc/v1/token for a short-lived access_token. The Databricks SDK does
this exchange for us when configured with the SP creds (auth_type='oauth-m2m').
"""
from __future__ import annotations

import os

from databricks.sdk.core import Config, oauth_service_principal


def m2m_bearer() -> str:
    host = os.environ["DATABRICKS_HOST"]
    cfg = Config(
        host=host,
        client_id=os.environ["DATABRICKS_CLIENT_ID"],
        client_secret=os.environ["DATABRICKS_CLIENT_SECRET"],
    )
    creds = oauth_service_principal(cfg)      # client_credentials against /oidc/v1/token
    return creds().token                       # the OAuth access_token (the Bearer)
```
> Verify the exact SDK surface at implementation time — the M2M helper name has varied across
> SDK versions (`oauth_service_principal(cfg)` returning a token supplier is the current
> shape). Fallback if that import path differs: build a `WorkspaceClient(config=cfg)` and read
> `w.config.oauth_token().access_token`, which forces the same grant.

### 4.4 `examples/m2m_test.py`
Happy path — mint bearer, call the deployed app, assert 200 + JSON:

```python
"""T3a happy-path test — run from your laptop against the DEPLOYED app.

Env required:
  DATABRICKS_HOST           https://adb-984752964297111.11.azuredatabricks.net
  APP_URL                   https://customer360-984752964297111.11.azure.databricksapps.com
  DATABRICKS_CLIENT_ID      the partner SP client_id
  DATABRICKS_CLIENT_SECRET  the partner SP client_secret (minted in step 2)
  CUSTOMER_ID               optional, defaults to a known id
"""
from __future__ import annotations

import json
import os
import sys

import requests

from _token import m2m_bearer


def main() -> int:
    app_url = os.environ["APP_URL"].rstrip("/")
    customer_id = os.environ.get("CUSTOMER_ID", "C0000000")
    bearer = m2m_bearer()

    resp = requests.get(
        f"{app_url}/api/external/customers/{customer_id}",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=30,
    )
    print(f"GET /api/external/customers/{customer_id} -> {resp.status_code}")
    print(json.dumps(resp.json(), indent=2, default=str))
    if resp.status_code != 200:
        print("FAILED: expected 200", file=sys.stderr)
        return 1
    print("OK: 200 + customer JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```
- Uses `requests` (add to a dev/examples requirement, or note `pip install requests` — it's
  not an app runtime dep, so it does NOT go in `app/requirements.txt`).
- The Bearer goes to the **Apps proxy**, which strips it and forwards
  `X-Forwarded-Access-Token`. We never call `/oidc` ourselves — the SDK helper did.

---

## 5. Grants / workspace setup (do BEFORE running the test)

Run as the deploying user (has admin). Values from master_plan §0 / memory.
**Primary path: a dedicated `cust360-partner` SP (DA1).** Steps 1-2 create it; steps 3-5 grant
it exactly the access the external endpoint needs — nothing more.

1. **Create the partner SP** (task step 1):
   ```
   databricks service-principals create --display-name cust360-partner
   ```
   Capture two ids from the response:
   - `id` — the **numeric** service-principal id (used for the secret + app permission).
   - `applicationId` — the **client_id** (a UUID; used as `DATABRICKS_CLIENT_ID` + as the
     grantee in the UC `GRANT` statements below).
   > Record both in memory once created (they replace the `<PARTNER_SP_ID>` /
   > `<PARTNER_CLIENT_ID>` placeholders used throughout the rest of this section).

2. **Mint its OAuth client_secret** (task step 2):
   ```
   databricks service-principal-secrets-proxy create <PARTNER_SP_ID>
   ```
   Save `secret` (the `client_secret`) — it is shown **once**. Together with the client_id from
   step 1, these become `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` for the test env.

3. **CAN_USE on the app for the partner SP** (task step 3 — the 401 gate):
   - UI: Workspace → Apps → `customer360` → Permissions → Add → Service principal →
     `cust360-partner` (or its `<PARTNER_CLIENT_ID>`) → **CAN_USE**.
   - Or CLI via the app permissions API (`databricks apps ... ` / permissions endpoint).
   - CAN_USE is the ONLY app permission the partner should ever hold (least-privilege, DA1).

4. **Warehouse CAN_USE for the partner SP** (task step 4 — one of the two data gates):
   - Warehouse `148ccb90800933a1` → Permissions → add `cust360-partner` → **CAN USE**.

5. **Gold reads for the partner SP** (task step 4 — the INSUFFICIENT_PERMISSIONS gate):
   ```sql
   GRANT USE CATALOG ON CATALOG ai_27 TO `<PARTNER_CLIENT_ID>`;
   GRANT USE SCHEMA  ON SCHEMA  ai_27.lakebase_apps_capstone_gold TO `<PARTNER_CLIENT_ID>`;
   GRANT SELECT ON TABLE ai_27.lakebase_apps_capstone_gold.customers    TO `<PARTNER_CLIENT_ID>`;
   GRANT SELECT ON TABLE ai_27.lakebase_apps_capstone_gold.transactions TO `<PARTNER_CLIENT_ID>`;
   ```
   > A fresh partner SP starts with **zero** access, so every one of these grants matters —
   > omit one and the test fails at exactly that gate (which is the point: it demonstrates the
   > boundary). Note we grant SELECT on only `customers` + `transactions` — the two tables the
   > endpoint reads — NOT the whole schema. The partner cannot see notes, overrides, audit, or
   > anything in Lakebase.
   >
   > **Fallback (app SP, DA1):** if SP creation is blocked, substitute the app SP client_id
   > `10c64b22-ac46-4123-bb60-041dc9d4fa92` everywhere above and skip steps 1-2 (mint its secret
   > with `service-principal-secrets-proxy create 144899163311454`). It likely already has the
   > gold grants (T7 job writes gold as this SP) — apply anyway, they're idempotent.

---

## 6. How to deploy & test

1. **Build (if any frontend change — there is none here, so skip `npm run build`).**
   T3a is backend + examples only; no React touched.
2. **Deploy** the backend change:
   ```
   databricks bundle deploy --target dev      # dev = source_code_path, fast iteration
   ```
   (Post-T8, prod is git-source and the submission source of truth. Iterate on dev; when
   green, commit + `bundle deploy -t prod` + `bundle run customer360 -t prod` — remember the
   Git Proxy cluster `0702-202607-z9tgxva7` must be RUNNING for the prod pull, gotcha #12.)
3. **Create the partner SP + apply grants** (§5) — one-time.
4. **Run the M2M test from your laptop:**
   ```
   export DATABRICKS_HOST=https://adb-984752964297111.11.azuredatabricks.net
   export APP_URL=https://customer360-984752964297111.11.azure.databricksapps.com
   export DATABRICKS_CLIENT_ID=<PARTNER_CLIENT_ID>       # cust360-partner applicationId (§5.1)
   export DATABRICKS_CLIENT_SECRET=<from §5.2>
   cd examples && python m2m_test.py
   ```
   **Working looks like:** `-> 200` then a pretty-printed `CustomerDetail` JSON
   (profile + recent_transactions), ending `OK: 200 + customer JSON`. Capture this stdout for
   the writeup (done-when #1).
5. **Confirm audit attribution** (done-when #2): query `system.query.history` for the
   statement and confirm the executor = the **partner SP** (`cust360-partner`), NOT the
   deploying user and NOT the app SP. E.g.:
   ```sql
   SELECT statement_text, executed_by, executed_by_user_id, statement_id, start_time
   FROM system.query.history
   WHERE statement_text ILIKE '%FROM ai_27.lakebase_apps_capstone_gold.customers%'
   ORDER BY start_time DESC LIMIT 5;
   ```
   With a dedicated partner SP, "attributed to the SP" shows `cust360-partner` — visibly a
   *partner* identity distinct from both the human deployer and the app's own SP. This is the
   cleanest possible evidence for the boundary (the reason DA1 prefers the dedicated SP).

### Expected failure modes (and what they teach)
| Symptom | Gate | Fix |
|---|---|---|
| Test → **401** | Apps proxy | SP lacks CAN_USE on the app (§5.3); or bad/expired bearer |
| Test → **403** (our friendly message) | warehouse OBO | SP token lacks `sql` scope — but M2M SPs get scope from grants, not consent; more likely the app's `user_api_scopes` |
| Test → **403 INSUFFICIENT_PERMISSIONS** wrapped as 403 | warehouse UC | SP missing warehouse CAN_USE (§5.4) or gold USE CATALOG/SCHEMA/SELECT (§5.5) |
| Test → **404** | handler | wrong `CUSTOMER_ID`; pick a real id (e.g. `C0000000`) |
| Local run → **401** always | no proxy | must hit the DEPLOYED `APP_URL`, not localhost (DA6) |

> **Open question to resolve during implementation:** does an M2M SP bearer carry the app's
> `user_api_scopes` (`sql`, `dashboards.genie`) the same way a browser OBO token does? For
> human OBO the scopes come from *consent*; an SP has no consent screen. If the warehouse call
> 403s on scope (not on UC grants), the fix is on the app's authorization config, not on UC.
> Verify empirically — this is a genuine learning point for the reflection.

---

## 7. Done-when checklist (from CAPSTONE_TASKS.md T3a)
- [x] `examples/m2m_test.py` returns **200** + the customer JSON; stdout captured for writeup.
      ✅ 2026-07-30 — `C0000000` profile (James Chen, S8, LTV 66750.76) + 20 txns, minted as
      the partner SP via client_credentials.
- [x] The handler reads gold via the warehouse using the caller's bearer — confirmed in the
      SQL audit log: the statement is attributed to the **SP**, not the deploying user.
      ✅ 2026-07-30 `system.query.history`: both SELECTs (customers + transactions) attributed
      to `executed_by ec2a70c0-…` / `executed_by_user_id 146608447413191` = `cust360-partner`
      (surfaced after ~2 min ingestion lag).
- [x] `app/backend/routers/external.py` exists under the `/api/external` prefix, uses
      `obo_client` (no SP fallback), never touches Lakebase. ✅
- [x] `examples/_token.py` + `examples/m2m_test.py` committed to the tree (+ `examples/README.md`).
- [x] Deployed — dev verified (`bundle deploy` + `bundle run --target dev` → new deployment
      serving `t3a-external`). ⏳ prod re-deploy for submission still to do.

**Provisioned (2026-07-30):** partner SP `cust360-partner` — numeric id `146608447413191`,
client_id `ec2a70c0-4ef1-443a-b2ca-1937cc8fa205`. Grants applied: CAN_USE on app + warehouse,
USE CATALOG/SCHEMA + SELECT on gold customers/transactions. OAuth secret minted (kept out of
repo; expires 2028-07-29).

## 8. Reflection points to capture (for the submission writeup)
- M2M vs. human OBO: **same proxy path, same handler code** — the only differences are token
  minting (client_credentials grant vs. browser SSO) and grants. Powerful demonstration that
  the Apps auth model is uniform.
- **Dedicated least-privilege partner SP** (`cust360-partner`): holds ONLY CAN_USE on the app
  + SELECT on `gold.customers`/`transactions`. It cannot reach Lakebase, cannot trigger the
  forward-ETL job, cannot manage the app — a real demonstration of the partner boundary, not
  the app impersonating itself.
- Two permission gates (proxy CAN_USE vs. warehouse/UC SELECT) fail with different errors —
  worth showing both in the writeup as evidence you understand the boundary. A fresh SP with
  zero starting access makes each gate observable.
- Audit attribution to `cust360-partner` (a distinct identity, not the deploying human or the
  app SP) proves the OBO (no-SP-fallback) contract held.

---

## 9. After T3a
Per master_plan §5: **T9** (Lakebase branching + PITR + query insights — screenshots), then
the **optimizations pass** (pooling upgrade, caching, React perf, observability), then
**submission** (reflection + 3-min recording + repo/app URLs + this task's stdout).
