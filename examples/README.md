# examples — external M2M API test (T3a)

Scripts that exercise the **external partner API** (`GET /api/external/customers/{id}`) using
**machine-to-machine (M2M)** auth: a service principal mints an OAuth bearer via the
`client_credentials` grant and calls the **deployed** app.

- `_token.py` — shared helper; runs the SDK M2M flow and returns the OAuth `access_token`.
- `m2m_test.py` — happy path; mints the bearer, GETs the endpoint, asserts `200` + JSON.

## Why deployed-only
The endpoint reads `X-Forwarded-Access-Token`, which only the Databricks Apps proxy injects.
A local uvicorn has no proxy → the handler 401s. So point `APP_URL` at the live app.

## Run
These are developer/test scripts — NOT app runtime deps (so `requests` is intentionally not
in `app/requirements.txt`). Use the app venv (already has `databricks-sdk` + `requests`) or
`pip install databricks-sdk requests`.

```bash
export DATABRICKS_HOST=https://adb-984752964297111.11.azuredatabricks.net
export APP_URL=https://customer360-984752964297111.11.azure.databricksapps.com
export DATABRICKS_CLIENT_ID=<cust360-partner client_id>
export DATABRICKS_CLIENT_SECRET=<cust360-partner secret>
# optional: export CUSTOMER_ID=C0000000
python m2m_test.py
```

Expected: `-> 200`, a pretty-printed `CustomerDetail` JSON, then `OK: 200 + customer JSON`.

## The `cust360-partner` service principal
A **dedicated least-privilege partner SP** (not the app's own SP), holding ONLY:
- `CAN_USE` on the `customer360` app (the Apps-proxy gate),
- `CAN_USE` on the SQL warehouse,
- `USE CATALOG` + `USE SCHEMA` + `SELECT` on `ai_27.lakebase_apps_capstone_gold.customers`
  and `.transactions` (nothing else — no notes/overrides/audit, no Lakebase).

The `client_secret` is not stored in this repo. Mint/rotate it with:
```bash
databricks service-principal-secrets-proxy create <partner_sp_numeric_id>
```
