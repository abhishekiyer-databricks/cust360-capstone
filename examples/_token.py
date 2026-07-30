"""M2M helper (T3a): run the OAuth client_credentials grant and return an OAuth *access_token*.

A partner service principal has no browser and no interactive login. It holds a
``client_id`` + ``client_secret`` and performs the OAuth 2.0 *client_credentials* grant:
POST those creds to ``https://<host>/oidc/v1/token`` and get back a short-lived OAuth
**access_token**. That access_token — NOT the client_secret — is the Bearer sent to the app.

The Databricks SDK does this exchange for us: ``oauth_service_principal(cfg)`` returns a
callable that mints (and caches/refreshes) the token and hands back the ready-to-use
``{"Authorization": "Bearer <access_token>"}`` header. We strip the scheme and return the raw
token so the caller controls how it's attached.

Env required:
  DATABRICKS_HOST           e.g. https://adb-984752964297111.11.azuredatabricks.net
  DATABRICKS_CLIENT_ID      the partner SP's client_id (applicationId)
  DATABRICKS_CLIENT_SECRET  the partner SP's OAuth client_secret
"""
from __future__ import annotations

import os

from databricks.sdk.core import Config, oauth_service_principal


def m2m_bearer() -> str:
    """Return the OAuth access_token for the SP configured via env (client_credentials grant)."""
    cfg = Config(
        host=os.environ["DATABRICKS_HOST"],
        client_id=os.environ["DATABRICKS_CLIENT_ID"],
        client_secret=os.environ["DATABRICKS_CLIENT_SECRET"],
    )
    # oauth_service_principal(cfg) -> callable that does the grant and returns auth headers.
    headers = oauth_service_principal(cfg)()
    authorization = headers["Authorization"]  # "Bearer <access_token>"
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise RuntimeError(f"unexpected Authorization header from M2M grant: {authorization!r}")
    return token
