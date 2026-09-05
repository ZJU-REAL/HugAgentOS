# -*- coding: utf-8 -*-
"""The assistant row a run writes into, and its in-flight marker.

The row is created empty when the run is admitted and refreshed while the run
streams, so the server always holds this turn's display state. The marker
stored on it records which run owns it and the last SSE event folded into it
(``event_offset``); a client resumes the stream from there.

Whether the row is *still* in flight is never stored — it is the liveness of
the owning run, read at the time of the query. A marker left behind by a run
that died is therefore harmless: the row simply reads as final.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from core.db.models import ChatMessage

IN_FLIGHT_KEY = "in_flight"


def mark(run_id: str, phase: str, event_offset: Optional[int] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"run_id": str(run_id), "phase": str(phase)}
    if event_offset is not None:
        payload["event_offset"] = int(event_offset)
    return payload


def marker(extra_data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(extra_data, dict):
        return None
    value = extra_data.get(IN_FLIGHT_KEY)
    return value if isinstance(value, dict) and value.get("run_id") else None


def new_assistant_row(
    *,
    message_id: str,
    chat_id: str,
    chat_seq: Optional[int],
    run_id: str,
    model: Optional[str],
    created_at: datetime,
) -> ChatMessage:
    """The empty assistant row every admission path creates alongside its run."""
    return ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_seq=chat_seq,
        role="assistant",
        content="",
        model=model,
        extra_data={
            IN_FLIGHT_KEY: mark(run_id, "accepted"),
            "message_id": message_id,
            "run_id": run_id,
        },
        created_at=created_at,
    )
