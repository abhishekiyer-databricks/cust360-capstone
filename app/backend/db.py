"""Lakebase (Postgres) access — always as the app service principal (T2).

``lakebase_sp()`` is a context manager that yields a live psycopg 3 connection to the
Lakebase instance, authenticated as the app SP:

    with lakebase_sp() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()

Two problems it solves:
- **Token expiry** — Lakebase OAuth tokens last ~1h. We mint a *fresh* token per call via
  ``generate_database_credential`` so it's never stale.
- **Right identity** — connects as ``user = SP_CLIENT_ID`` (the app SP's client_id, which
  is the Postgres role granted SELECT/INSERT/UPDATE in T1-b). NOT ``current_user()``.

There is intentionally no ``lakebase_obo()``: Lakebase doesn't support OBO scopes
(``generate_database_credential`` with a user bearer fails on the ``postgres`` scope).

DESIGN NOTE (T2 = correctness first): this opens one short-lived connection per call — a
guaranteed-fresh token, obviously correct, but unpooled. The Optimizations pass
(master_plan §7 / §3-D4) upgrades this to a ``psycopg_pool.ConnectionPool`` (size 2-10)
that rotates the token per new physical connection (e.g. ``max_lifetime`` < 1h, or a
per-connect factory). ``psycopg[pool]`` is already a dependency so that's a drop-in change.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from . import config
from .auth import sp_client


def _mint_token() -> str:
    """Fresh Lakebase Postgres OAuth token for the app SP (valid ~1h)."""
    cred = sp_client().database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[config.PG_INSTANCE_NAME],
    )
    return cred.token


@contextmanager
def lakebase_sp() -> Iterator[psycopg.Connection]:
    """Yield a psycopg 3 connection to Lakebase as the app SP; always closed on exit."""
    conn = psycopg.connect(
        host=config.PGHOST,
        port=config.PGPORT,
        dbname=config.PGDATABASE,
        user=config.SP_CLIENT_ID,
        password=_mint_token(),
        sslmode="require",
    )
    try:
        yield conn
    finally:
        conn.close()
