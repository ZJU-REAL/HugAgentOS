"""Query and serialization helpers shared by the call-log routes.

Used by both the personal logs (``me_logs.py``, present in every edition) and the
EE admin console logs (``admin_logs.py``), so it must not live in an ``admin_*``
module: those are dropped from the CE tree.
"""

from typing import Any, Dict, List, Optional

from core.db.models import ChatSession
from sqlalchemy.orm import Session

_DATETIME_FIELDS = ("started_at", "completed_at", "created_at")


def row_to_dict(row: Any, *, session_title: Optional[str] = None) -> Dict[str, Any]:
    """Convert a SQLAlchemy row to a JSON-serializable dict.  Datetime
    columns are rendered via ``isoformat``; an optional ``session_title``
    is injected (when present) as an extra field."""
    data: Dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if col.name in _DATETIME_FIELDS and val is not None:
            val = val.isoformat()
        data[col.name] = val
    if session_title is not None or "chat_id" in data:
        data["session_title"] = session_title
    return data


def session_title_map(db: Session, chat_ids: List[str]) -> Dict[str, str]:
    if not chat_ids:
        return {}
    rows = (
        db.query(ChatSession.chat_id, ChatSession.title)
        .filter(ChatSession.chat_id.in_(list(set(chat_ids))))
        .all()
    )
    return {r.chat_id: r.title for r in rows}


def serialize_log(row, titles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    title = (titles or {}).get(getattr(row, "chat_id", None)) if titles is not None else None
    return row_to_dict(row, session_title=title)


def apply_filters(query, mapping: Dict[Any, Any]):
    """Apply each (column → value) pair as an equality filter, skipping ``None`` entries."""
    for col, value in mapping.items():
        if value is None:
            continue
        query = query.filter(col == value)
    return query


def apply_date_range(query, col, date_from, date_to):
    if date_from:
        query = query.filter(col >= date_from)
    if date_to:
        query = query.filter(col <= date_to)
    return query
