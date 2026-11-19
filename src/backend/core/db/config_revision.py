"""Revision rows in ``content_blocks``: one opaque token per configuration domain.

A writer bumps the token inside the transaction that changes the configuration;
readers in any worker compare it against the token their cache was built at.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select


def read_revision(db, key: str) -> str:
    """Current token ("" before the first bump). ``db`` is a Session or Connection."""
    from core.db.models import ContentBlock

    payload = db.execute(select(ContentBlock.payload).where(ContentBlock.id == key)).scalar_one_or_none()
    return str((payload or {}).get("revision", ""))


def bump_revision(db, key: str, updated_by: str) -> None:
    """Upsert a fresh token in the caller's transaction (Session or Connection)."""
    from core.db.models import ContentBlock

    dialect = db.get_bind().dialect.name if hasattr(db, "get_bind") else db.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Unsupported configuration database: {dialect}")
    values = {
        "payload": {"revision": uuid.uuid4().hex},
        "updated_at": datetime.now(timezone.utc),
        "updated_by": updated_by,
    }
    statement = insert(ContentBlock.__table__).values(id=key, **values)
    db.execute(statement.on_conflict_do_update(index_elements=["id"], set_=values))
