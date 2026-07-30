"""Lakebase (Postgres) access — always as the app service principal (T2 + Optimizations O1/O3).

``lakebase_sp()`` is a context manager that yields a live psycopg 3 connection to the
Lakebase instance, authenticated as the app SP:

    with lakebase_sp() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()

Two problems it solves:
- **Token expiry** — Lakebase OAuth tokens last ~1h. We mint a *fresh* token every time a
  *physical* connection is opened (see ``_FreshTokenConnection``) so it's never stale, and
  cap ``max_lifetime`` well below the token TTL so pooled connections are recycled (and
  re-authed) long before their token would expire.
- **Right identity** — connects as ``user = SP_CLIENT_ID`` (the app SP's client_id, which
  is the Postgres role granted SELECT/INSERT/UPDATE in T1-b). NOT ``current_user()``.

There is intentionally no ``lakebase_obo()``: Lakebase doesn't support OBO scopes
(``generate_database_credential`` with a user bearer fails on the ``postgres`` scope).

OPTIMIZATIONS PASS (master_plan §7 / §3-D4, plan O1/O3): this now checks connections out of
a module-level ``psycopg_pool.ConnectionPool`` (size 2-10) instead of dialing a fresh TLS
socket + minting a credential on every request. Token rotation is per *physical* connection
(``_FreshTokenConnection.connect`` mints a new token each time the pool opens a socket);
``max_lifetime`` recycles connections every 30 min (< the ~1h token TTL). Outbound safety:
``connect_timeout`` bounds the dial, and a per-connection ``statement_timeout`` stops a
runaway query from pinning a worker. ``lakebase_sp()`` keeps the exact same context-manager
API, so no router changes are needed — it just checks out of the pool.

The pool is constructed with ``open=False`` (construction does not connect), so importing
this module never fails locally when SP creds are absent; ``open_pool()`` / ``close_pool()``
are driven by the FastAPI lifespan in ``main.py``.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

from . import config
from .auth import sp_client

log = logging.getLogger(__name__)

# Pool sizing (master_plan §3-D4). Recycle connections every 30 min — comfortably below the
# ~1h Lakebase token TTL, so a pooled socket is torn down and re-authed before its token dies.
_POOL_MIN_SIZE = 2
_POOL_MAX_SIZE = 10
_MAX_LIFETIME_S = 1800  # 30 min
_CONNECT_TIMEOUT_S = 10
_STATEMENT_TIMEOUT_MS = 15000  # 15s — a single Lakebase query can't pin a worker forever


def _mint_token() -> str:
    """Fresh Lakebase Postgres OAuth token for the app SP (valid ~1h)."""
    cred = sp_client().database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[config.PG_INSTANCE_NAME],
    )
    return cred.token


class _FreshTokenConnection(psycopg.Connection):
    """psycopg Connection that mints a fresh Lakebase token at every physical connect.

    The pool calls ``connection_class.connect(conninfo, **kwargs)`` whenever it opens a new
    socket (startup, growth, or ``max_lifetime`` recycle). We inject a just-minted token as
    the password here so a recycled connection never reuses a stale/expired credential —
    this is option (a) from master_plan §3-D4 (fresh token per physical connection).
    """

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
        kwargs["password"] = _mint_token()
        return super().connect(conninfo, **kwargs)


def _configure(conn: psycopg.Connection) -> None:
    """Run once per new physical connection: bound how long a single statement may run."""
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
    conn.commit()


def _build_pool() -> ConnectionPool:
    """Construct the pool WITHOUT connecting (open=False) so import is always safe locally."""
    return ConnectionPool(
        # conninfo has everything except the password, which _FreshTokenConnection injects.
        conninfo=(
            f"host={config.PGHOST} port={config.PGPORT} dbname={config.PGDATABASE} "
            f"user={config.SP_CLIENT_ID} sslmode=require"
        ),
        connection_class=_FreshTokenConnection,
        kwargs={"connect_timeout": _CONNECT_TIMEOUT_S},
        configure=_configure,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        max_lifetime=_MAX_LIFETIME_S,
        open=False,
        name="lakebase_sp",
    )


# Module-level singleton. Construction does not connect (open=False); open_pool() connects.
_pool: ConnectionPool = _build_pool()


def open_pool() -> None:
    """Open the pool on app startup (FastAPI lifespan). Idempotent-ish; logs on failure."""
    try:
        _pool.open(wait=True, timeout=30)
        log.info("lakebase pool opened (min=%d max=%d)", _POOL_MIN_SIZE, _POOL_MAX_SIZE)
    except Exception:
        # Don't crash the whole app if Lakebase is briefly unreachable at boot — the first
        # request will surface the error, and the pool self-heals as the instance recovers.
        log.exception("lakebase pool failed to open at startup")


def close_pool() -> None:
    """Close the pool on app shutdown (FastAPI lifespan)."""
    _pool.close()
    log.info("lakebase pool closed")


@contextmanager
def lakebase_sp() -> Iterator[psycopg.Connection]:
    """Yield a psycopg 3 connection to Lakebase as the app SP, checked out of the pool.

    Same API as before (a context manager) so callers are unchanged. The connection is
    returned to the pool on exit; the pool handles token rotation + recycling.
    """
    with _pool.connection() as conn:
        yield conn
