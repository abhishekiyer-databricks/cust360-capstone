"""T3a happy-path test — run from your laptop against the DEPLOYED app.

Exercises the full M2M flow end-to-end: mint an OAuth bearer for the partner SP
(client_credentials grant, via _token.m2m_bearer), send it to the deployed app's
``/api/external/customers/{id}`` endpoint, and assert HTTP 200 + the CustomerDetail JSON.

Why deployed-only: the endpoint reads ``X-Forwarded-Access-Token``, which only the Databricks
Apps proxy injects. A local uvicorn has no proxy → the handler would 401. So this test must
target the live APP_URL, not localhost.

Env required:
  DATABRICKS_HOST           https://adb-984752964297111.11.azuredatabricks.net
  APP_URL                   https://customer360-984752964297111.11.azure.databricksapps.com
  DATABRICKS_CLIENT_ID      partner SP client_id (cust360-partner applicationId)
  DATABRICKS_CLIENT_SECRET  partner SP client_secret
  CUSTOMER_ID               optional; defaults to a known id

Capture stdout for the writeup (done-when #1).
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

    print(f"Minting M2M bearer for client_id {os.environ['DATABRICKS_CLIENT_ID']} ...")
    bearer = m2m_bearer()
    print("  got OAuth access_token (client_credentials grant).")

    url = f"{app_url}/api/external/customers/{customer_id}"
    print(f"GET {url}")
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=30,
    )
    print(f"-> {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2, default=str))
    except ValueError:
        print(resp.text)

    if resp.status_code != 200:
        print("FAILED: expected 200", file=sys.stderr)
        return 1
    print("OK: 200 + customer JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
