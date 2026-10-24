"""User and environment scoped navigation. Independent of installs and permissions."""

from features.workbench.coordination import runtime


import re
from copy import deepcopy
from hashlib import sha256

from features.workbench import external
from core.config.local_mode import local_mode_enabled
from core.db.models import ContentBlock
from core.infra.time import utc_now
from sqlalchemy import update


def identity(user_id, environment):
    return "workbench_navigation:" + sha256((user_id + "\0" + environment).encode()).hexdigest()


def read(db, user_id, environment):
    row = db.get(ContentBlock, identity(user_id, environment))
    return (
        deepcopy(row.payload)
        if row
        else {"revision": 0, "pinned": None, "order": [], "environment": environment}
    )


def save(db, user_id, environment, body):
    # Retain temporarily unavailable references; syntactic validity is not a grant.
    for values in [body["pinned"] or [], body["order"]]:
        if not local_mode_enabled() and any(key.startswith("plugin:local:") for key in values):
            external.fail(422, "unsupported_reference_origin")
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"(?:builtin|external|legacy|plugin):[a-zA-Z0-9_.:-]{1,240}", key)
            for key in values
        ):
            external.fail(422, "invalid_navigation_preferences")
    runtime(db, user_id)
    current = read(db, user_id, environment)
    if body["revision"] != current["revision"]:
        db.rollback()
        external.fail(409, "revision_conflict")
    value = {**body, "environment": environment, "revision": current["revision"] + 1}
    key = identity(user_id, environment)
    now = utc_now()
    if current["revision"] == 0:
        db.add(ContentBlock(id=key, payload=value, updated_by=user_id, updated_at=now))
    else:
        count = db.execute(
            update(ContentBlock)
            .where(
                ContentBlock.id == key,
                ContentBlock.payload["revision"].as_integer() == current["revision"],
            )
            .values(payload=value, updated_by=user_id, updated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if count != 1:
            db.rollback()
            external.fail(409, "revision_conflict")
    db.commit()
    db.expire_all()
    return value
