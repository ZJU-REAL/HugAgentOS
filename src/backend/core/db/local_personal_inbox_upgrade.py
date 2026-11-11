"""The data half of ``ce_0037_personal_router`` for local databases that never run Alembic.

The metadata reconciler adds the router's ``kind`` / ``chat_id`` / ``turn_id`` columns, but
not what the revision does to rows written before them:

- a Trigger that was mid-flight under the old router (``responding`` / ``routing``, or
  ``claimed`` without a Turn) is claimed by no Turn and would wait forever; it goes back
  to ``pending`` so the scheduler picks it up again;
- the Trigger kind used to live only in the payload, so old heartbeat, bootstrap and task
  events would be answered as user messages under the column's ``message`` default.

The revision also drops ``decision.attempts`` and the old lease columns; nothing reads
them any more, so they are left alone here.

Every step only touches rows the current router can never write (it always sets
``kind``, and ``turn_id`` together with a ``claimed`` status), so running it on each start
is safe; that is also what repairs rows a rolled-back older release wrote in between.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

INBOX = "personal_message_inbox"


def inbox_kind(payload: dict) -> str:
    """The Trigger kind an old inbox row stood for (same rule as ``ce_0037``)."""
    if payload.get("kind") == "heartbeat":
        return "heartbeat"
    if payload.get("kind") == "bootstrap":
        return "bootstrap"
    if payload.get("kind") == "task_event":
        return "session_run" if payload.get("event") == "session_run_finished" else "task_result"
    return "message"


def _json(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def reconcile_local_personal_inbox(bind: Engine) -> dict[str, list[str]]:
    report: dict[str, list[str]] = {"requeued": [], "kinds": []}
    with bind.begin() as connection:
        if not inspect(connection).has_table(INBOX):
            return report
        columns = {item["name"] for item in inspect(connection).get_columns(INBOX)}
        if not {"kind", "chat_id", "turn_id"} <= columns:
            return report
        connection.execute(text(
            "UPDATE personal_message_inbox SET chat_id = (SELECT chat_id FROM chat_messages "
            "WHERE chat_messages.message_id = personal_message_inbox.message_id) "
            "WHERE chat_id IS NULL"
        ))
        requeued = connection.execute(text(
            "UPDATE personal_message_inbox SET status = 'pending' "
            "WHERE turn_id IS NULL AND status IN ('responding', 'routing', 'claimed')"
        )).rowcount
        if requeued:
            report["requeued"].append(f"{INBOX}:{requeued}")

        changes = []
        for message_id, payload in connection.execute(text(
            # Every row, whatever its status, as ce_0037 does: a failed or finished row can
            # be reprocessed later, and heartbeat cleanup selects rows by kind. The current
            # router always writes kind, and user-message payloads carry no "kind" key.
            "SELECT message_id, payload FROM personal_message_inbox WHERE kind = 'message'"
        )):
            kind = inbox_kind(_json(payload))
            if kind != "message":
                changes.append({"message_id": message_id, "kind": kind})
        if changes:
            connection.execute(text(
                "UPDATE personal_message_inbox SET kind = :kind WHERE message_id = :message_id"
            ), changes)
            report["kinds"].append(f"{INBOX}:{len(changes)}")
    return report


__all__ = ["inbox_kind", "reconcile_local_personal_inbox"]
