"""Genie chat endpoints (T5) — natural-language Q&A over the gold data, via OBO.

Three thin endpoints wrap Databricks' async **Conversation API**:

- POST /api/genie/conversations                         → start a conversation (first question)
- POST /api/genie/conversations/{cid}/messages          → follow-up in the same conversation
- GET  /api/genie/conversations/{cid}/messages/{mid}    → poll one message until terminal

All three run **OBO (on-behalf-of the calling user)** via `obo_client(request)` — so Genie's
own row/column security and audit reflect the real user, not the app SP (master_plan §3-D2).
No OBO token → 401 (inherited from obo_client); the `dashboards.genie` scope is granted in
resources/app.yml.

Design: the backend is **stateless**. Genie's API is async (submit a message, then poll), so
the *frontend* owns conversation_id + message_id and drives the poll loop (typing indicator,
~30s cap). Each GET here is a single `get_message` call — no request hangs waiting. Reusing
conversation_id across messages is what preserves context (the 2nd done-when).
"""
from __future__ import annotations

import logging

from databricks.sdk.errors import PermissionDenied
from fastapi import APIRouter, HTTPException, Request

from .. import config
from ..auth import obo_client
from ..models import GenieAsk, GenieMessage, GenieMessageRef, GenieResult

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/genie", tags=["genie"])

# Statuses past which a message won't change — the client stops polling here.
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}


def _space_id() -> str:
    """The Genie space id, or 503 if it isn't configured."""
    if not config.GENIE_SPACE_ID:
        raise HTTPException(
            status_code=503,
            detail="Genie space id is not configured (GENIE_SPACE_ID).",
        )
    return config.GENIE_SPACE_ID


def _status_value(status) -> str:
    """MessageStatus enum → its string value (e.g. 'COMPLETED'); '' if absent."""
    if status is None:
        return ""
    return status.value if hasattr(status, "value") else str(status)


def _text_answer(msg) -> str | None:
    """Pull the text answer out of a message's attachments (if it answered in prose)."""
    parts: list[str] = []
    for att in msg.attachments or []:
        if att.text and att.text.content:
            parts.append(att.text.content)
    # Some responses put a plain summary on msg.content instead of a text attachment.
    if not parts and msg.content:
        parts.append(msg.content)
    return "\n\n".join(parts) if parts else None


def _query_attachment_id(msg) -> tuple[str | None, str | None]:
    """First query attachment's (attachment_id, sql) — Genie answered by running SQL."""
    for att in msg.attachments or []:
        if att.query is not None:
            return att.attachment_id, att.query.query
    return None, None


def _to_result(resp) -> GenieResult | None:
    """Map a GenieGetMessageQueryResultResponse (a SQL statement response) to columns+rows.

    Caps the preview at 50 rows — this is a chat widget, not a data grid.
    """
    sr = getattr(resp, "statement_response", None)
    if sr is None or sr.result is None or sr.manifest is None:
        return None
    schema = sr.manifest.schema
    columns = [c.name for c in (schema.columns or [])] if schema else []
    data = sr.result.data_array or []
    truncated = len(data) > 50 or bool(getattr(sr.manifest, "truncated", False))
    rows = [list(r) for r in data[:50]]
    return GenieResult(columns=columns, rows=rows, truncated=truncated)


@router.post("/conversations", response_model=GenieMessageRef)
def start_conversation(body: GenieAsk, request: Request):
    """Start a new conversation with the first question; return handles to poll."""
    w = obo_client(request)
    try:
        # start_conversation returns a Wait[GenieMessage]; .response is the initial message
        # (status usually still in-flight). We DON'T block — the client polls the GET below.
        waiter = w.genie.start_conversation(space_id=_space_id(), content=body.content)
        msg = waiter.response
    except PermissionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail=(
                "OBO token lacks the Genie scope, or you don't have access to the Genie "
                f"space. If this is the first time, re-consent in the app. ({exc})"
            ),
        )
    except Exception as exc:
        log.exception("genie start_conversation failed")
        raise HTTPException(status_code=502, detail=f"Could not start Genie conversation: {exc}")
    return GenieMessageRef(conversation_id=msg.conversation_id, message_id=msg.message_id)


@router.post("/conversations/{conversation_id}/messages", response_model=GenieMessageRef)
def create_message(conversation_id: str, body: GenieAsk, request: Request):
    """Follow-up question in an existing conversation (context preserved via conversation_id)."""
    w = obo_client(request)
    try:
        waiter = w.genie.create_message(
            space_id=_space_id(), conversation_id=conversation_id, content=body.content
        )
        msg = waiter.response
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=f"Genie access denied: {exc}")
    except Exception as exc:
        log.exception("genie create_message failed")
        raise HTTPException(status_code=502, detail=f"Could not send Genie message: {exc}")
    return GenieMessageRef(conversation_id=conversation_id, message_id=msg.message_id)


@router.get("/conversations/{conversation_id}/messages/{message_id}", response_model=GenieMessage)
def get_message(conversation_id: str, message_id: str, request: Request):
    """Poll one message. When terminal + it has a query attachment, fetch the result rows."""
    w = obo_client(request)
    space_id = _space_id()
    try:
        msg = w.genie.get_message(
            space_id=space_id, conversation_id=conversation_id, message_id=message_id
        )
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=f"Genie access denied: {exc}")
    except Exception as exc:
        log.exception("genie get_message failed")
        raise HTTPException(status_code=502, detail=f"Could not fetch Genie message: {exc}")

    status = _status_value(msg.status)
    out = GenieMessage(
        status=status,
        content=_text_answer(msg),
        error=msg.error.error if msg.error else None,
    )

    # Only chase the query result once the message is COMPLETED — earlier, the attachment
    # result isn't ready and would error.
    if status == "COMPLETED":
        attachment_id, sql = _query_attachment_id(msg)
        out.query = sql
        if attachment_id:
            try:
                resp = w.genie.get_message_attachment_query_result(
                    space_id=space_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    attachment_id=attachment_id,
                )
                out.result = _to_result(resp)
            except Exception:
                # A missing/expired result shouldn't blank the text answer — log + move on.
                log.warning("genie attachment result fetch failed", exc_info=True)
    return out
