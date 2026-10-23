"""Persist ontology enforcement, activation and review events."""
from __future__ import annotations
from core.infra.time import utc_now
import uuid
from typing import Any
from core.db.repository import OntologyRepository

def record_enforcement_event(data: dict[str, Any]) -> None:
    """Persist an event from middleware/reviewer using an isolated session."""
    from core.db.engine import SessionLocal

    db = SessionLocal()
    try:
        payload = dict(data)
        payload.setdefault("event_id", f"ontoe_{uuid.uuid4().hex[:16]}")
        OntologyRepository(db).create_event(payload)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def record_runtime_activation(
    event: dict[str, Any],
    runtime: dict[str, Any],
    *,
    user_id: str | None,
    chat_id: str | None,
) -> None:
    """Persist one asset-driven workflow activation as append-only evidence."""

    version_id = next(
        (
            pack.get("version_id")
            for pack in runtime.get("packs", [])
            if pack.get("pack_id") == event.get("pack_id")
        ),
        None,
    )
    record_enforcement_event(
        {
            "user_id": user_id,
            "chat_id": chat_id,
            "pack_id": event.get("pack_id"),
            "version_id": version_id,
            "rule_id": f"activation:{event.get('workflow_id')}",
            "stage": "checkpoint",
            "event_type": "runtime_activation",
            "decision": "pass",
            "mode": "enforce",
            "target": f"{event.get('asset_kind')}:{event.get('asset_id')}",
            "details": event,
        }
    )


def record_review_run(data: dict[str, Any]) -> None:
    from core.db.engine import SessionLocal

    db = SessionLocal()
    try:
        payload = dict(data)
        payload.setdefault("review_id", f"ontor_{uuid.uuid4().hex[:16]}")
        payload.setdefault("created_at", utc_now())
        OntologyRepository(db).create_review(payload)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
