"""Persist the turn's evolution summary onto the message.

The summary lives in ``chat_messages.metadata`` under an ``evolution`` key,
exactly alongside ``ontology_governance``.  That placement is the whole reason
the card survives a page reload and reaches a client that had already
disconnected when settlement finished: the transcript is the durable channel,
the SSE push is only the fast one.

An ``empty`` settlement **is** persisted, as a terminal marker carrying no
entries.  It renders nothing — the transcript shows no card for a turn that
wrote no memory — but a client polling for that turn now gets a terminal answer
instead of ``pending`` forever.  Leaving it unwritten is what made every
ordinary turn end at "本轮进化结算未完成": the poller ran out of attempts waiting
for a row that was never going to be written, and reported the timeout as a
failure.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.db.engine import SessionLocal
from core.evolution.settlement import EvolutionSummary

logger = logging.getLogger(__name__)

METADATA_KEY = "evolution"


def persist_summary(message_id: str, summary: EvolutionSummary) -> bool:
    """Write the summary onto the assistant message. Never raises."""
    if not message_id or summary is None:
        return False

    try:
        from core.db.models import ChatMessage

        with SessionLocal() as db:
            message = (
                db.query(ChatMessage).filter(ChatMessage.message_id == message_id).first()
            )
            if message is None:
                return False
            # JSON columns need a fresh object for SQLAlchemy to notice the change.
            extra = dict(message.extra_data or {})
            extra[METADATA_KEY] = summary.to_dict()
            message.extra_data = extra
            db.commit()
            return True
    except Exception as exc:
        logger.warning("[evolution] persisting summary failed for %s: %s", message_id, exc)
        return False


def load_summary(message_id: str) -> Optional[Dict[str, Any]]:
    """Read back a persisted summary, for the settlement-query endpoint."""
    if not message_id:
        return None
    try:
        from core.db.models import ChatMessage

        with SessionLocal() as db:
            message = (
                db.query(ChatMessage).filter(ChatMessage.message_id == message_id).first()
            )
            if message is None:
                return None
            return (message.extra_data or {}).get(METADATA_KEY)
    except Exception as exc:
        logger.warning("[evolution] loading summary failed for %s: %s", message_id, exc)
        return None


def drop_entry(message_id: str, handle: str) -> bool:
    """Remove one memory from a persisted card, after the user deleted it.

    The card is a durable record of what the turn wrote. Once the user deletes
    one of those memories, leaving it on the card would show a memory that no
    longer exists — clicking it again would fail, and the count would keep
    claiming something the store cannot produce.
    """
    if not message_id or not handle:
        return False
    try:
        from core.db.models import ChatMessage

        with SessionLocal() as db:
            message = (
                db.query(ChatMessage).filter(ChatMessage.message_id == message_id).first()
            )
            if message is None:
                return False
            extra = dict(message.extra_data or {})
            summary = dict(extra.get(METADATA_KEY) or {})
            entries = [
                e for e in (summary.get("entries") or []) if e.get("handle") != handle
            ]
            if len(entries) == len(summary.get("entries") or []):
                return False
            summary["entries"] = entries
            summary["gain"] = len(entries)
            if not entries:
                # Nothing left to show — the card disappears rather than
                # lingering as an empty shell.
                summary["state"] = "empty"
            extra[METADATA_KEY] = summary
            message.extra_data = extra
            db.commit()
            return True
    except Exception as exc:
        logger.warning("[evolution] dropping entry failed for %s: %s", message_id, exc)
        return False


def update_entry_text(message_id: str, handle: str, text: str) -> bool:
    """Rewrite one memory's text on a persisted card, after the user edited it."""
    if not message_id or not handle or not text:
        return False
    try:
        from core.db.models import ChatMessage

        with SessionLocal() as db:
            message = (
                db.query(ChatMessage).filter(ChatMessage.message_id == message_id).first()
            )
            if message is None:
                return False
            extra = dict(message.extra_data or {})
            summary = dict(extra.get(METADATA_KEY) or {})
            entries = [dict(e) for e in (summary.get("entries") or [])]
            hit = False
            for item in entries:
                if item.get("handle") == handle:
                    item["text"] = text
                    item["action"] = "edited"
                    hit = True
            if not hit:
                return False
            summary["entries"] = entries
            extra[METADATA_KEY] = summary
            message.extra_data = extra
            db.commit()
            return True
    except Exception as exc:
        logger.warning("[evolution] updating entry failed for %s: %s", message_id, exc)
        return False
