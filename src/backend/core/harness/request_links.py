"""Which HTTP requests acted on a run: the bridge from a run_id to request trace_ids.

A run outlives the request that started it and is later steered, answered or cancelled by
other requests, each with its own trace_id. Every such request appends one ``request`` event
to the run's append-only event log, so a run's execution trace can list the trace_ids to look
up in the application log. Code running inside a run's own worker task is not a request and
never records a link, even though the task inherited the starting request's context.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.db.models import HarnessEventLog
from core.db.threads import run_db
from core.harness.events import Event
from core.harness.ledger import DurableEventStore
from core.infra.logging import get_logger, run_id_var, trace_id_var

logger = get_logger(__name__)

REQUEST_EVENT = "request"


def current_request_trace() -> str:
    """The trace_id of the HTTP request being served; empty inside a run's worker task."""
    return "" if run_id_var.get() else trace_id_var.get()


async def link_request(run_id: str, action: str, *, trace_id: Optional[str] = None) -> None:
    """Record that the current request (or ``trace_id``) performed ``action`` on ``run_id``.

    Best effort: a missing link only costs the operator a log lookup, so it never fails the
    request that acted on the run.
    """
    trace_id = current_request_trace() if trace_id is None else trace_id
    if not run_id or not trace_id:
        return
    event = Event.create(
        run_id=run_id, event_type=REQUEST_EVENT, phase="request",
        payload={"trace_id": trace_id, "action": action},
    )
    try:
        await run_db(DurableEventStore(), event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_request_link_failed", run_id=run_id, action=action, error=str(exc))


def list_request_links(db: Session, run_id: str) -> List[Dict[str, Any]]:
    rows = (
        db.query(HarnessEventLog.payload, HarnessEventLog.created_at)
        .filter(HarnessEventLog.run_id == run_id, HarnessEventLog.event_type == REQUEST_EVENT)
        .order_by(HarnessEventLog.event_seq)
        .all()
    )
    return [
        {"trace_id": payload.get("trace_id"), "action": payload.get("action"), "at": created_at}
        for payload, created_at in rows
    ]
