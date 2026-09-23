"""Model configuration revision, committed atomically with configuration writes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from core.db.models import ContentBlock

REVISION_KEY = "model_config_revision"


def read_revision(db: Session) -> str:
    payload = db.execute(
        select(ContentBlock.payload).where(ContentBlock.id == REVISION_KEY)
    ).scalar_one_or_none()
    return str((payload or {}).get("revision", ""))


def bump_revision(db: Session) -> None:
    # Both supported databases provide transactional upsert. The row also
    # serializes concurrent configuration commits; it never publishes ahead
    # of the provider/role changes in this same transaction.
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif db.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Unsupported model configuration database")
    values = {
        "payload": {"revision": uuid.uuid4().hex},
        "updated_at": datetime.now(timezone.utc),
        "updated_by": "model_config",
    }
    statement = insert(ContentBlock).values(id=REVISION_KEY, **values)
    db.execute(statement.on_conflict_do_update(index_elements=[ContentBlock.id], set_=values))
