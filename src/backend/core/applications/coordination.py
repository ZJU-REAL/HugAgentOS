"""Transaction locks separate shared configuration from per-user runtime writes.

PostgreSQL runtime transactions share the configuration barrier and exclusively
lock their owner. Configuration and collection use the exclusive barrier.
SQLite inherently serializes writers; scoped rows preserve that transaction rule.
"""

from hashlib import sha256

from core.db.models import ContentBlock
from core.infra.time import utc_now
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert

_NAMESPACE = "workbench:configuration"


def _key(value):
    return int.from_bytes(sha256(value.encode()).digest()[:8], "big", signed=True)


def _sqlite(db, scope):
    now = utc_now()
    db.execute(
        insert(ContentBlock)
        .values(
            id="workbench_lock:" + sha256(scope.encode()).hexdigest(), payload={}, updated_at=now
        )
        .on_conflict_do_update(index_elements=[ContentBlock.id], set_={"updated_at": now})
    )


def configuration(db):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _key(_NAMESPACE)})
    else:
        _sqlite(db, _NAMESPACE)


def runtime(db, user_id):
    if not user_id:
        raise ValueError("Runtime coordination requires an owner")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": _key(_NAMESPACE)})
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _key("workbench:user:" + user_id)}
        )
    else:
        _sqlite(db, "workbench:user:" + user_id)
