"""Structured JSON logging (Optimizations O4, master_plan §7 observability).

One line per log record, emitted as JSON: ``{ts, level, logger, msg, request_id, ...}``.
Stdlib only — no new dependency. The Apps runtime captures stdout, so
``databricks apps logs customer360`` shows these lines directly; JSON makes them greppable
and machine-parseable (vs the old plain ``%(asctime)s %(levelname)s ...`` format).

``request_id`` is carried on a ``contextvar`` set by the request-id middleware (main.py), so
every log line emitted while handling a request is automatically correlated to it —
React → FastAPI → Lakebase — without threading the id through every function.

Any ``extra={...}`` passed to a ``log.*`` call (e.g. slow-query params) is merged into the
JSON object, so ``log.warning("slow query", extra={"params": {...}})`` just works.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging

# Set per request by RequestIdMiddleware; read by the formatter. Default "-" outside requests.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# LogRecord attributes that are built-ins (not caller-supplied `extra`), so we can detect the
# extra fields to merge into the JSON output.
_STANDARD_ATTRS = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single JSON line, including any `extra` fields + request_id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _dt.datetime.fromtimestamp(record.created, _dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge caller-supplied extras (e.g. {"params": {...}, "elapsed_ms": 812}).
        for key, val in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = val
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger (replaces basicConfig)."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
