"""Administrator layout policy. It narrows discovery; it never grants execution."""

from features.workbench.coordination import configuration


from copy import deepcopy
from uuid import uuid4

from features.workbench import external
from core.db.models import ContentBlock
from core.infra.time import utc_now
from sqlalchemy import update

KEY = "workbench_policy"


def read(db):
    row = db.get(ContentBlock, KEY)
    return (
        deepcopy(row.payload)
        if row
        else {"revision": 0, "defaultPinned": [], "forced": [], "hidden": []}
    )


def save(db, body, actor, trace):
    from features.workbench.catalog import definitions

    configuration(db)
    known = {item["applicationKey"] for item in definitions(db)}
    placement = body["defaultPinned"] + body["forced"]
    fixed_utilities = {
        "builtin:chat",
        "builtin:settings",
        "builtin:docs",
        "builtin:plan_mode",
    }
    if set(placement + body["hidden"]) & fixed_utilities:
        external.fail(422, "unsupported_policy_placement")
    keys = placement + body["hidden"]
    if any(key not in known or key.startswith("plugin:") for key in keys):
        external.fail(422, "invalid_policy_application")
    if set(body["hidden"]) & (set(body["forced"]) | set(body["defaultPinned"])):
        external.fail(422, "conflicting_policy")
    current = read(db)
    if body["revision"] != current["revision"]:
        db.rollback()
        external.fail(409, "revision_conflict")
    now = utc_now()
    value = {**body, "revision": current["revision"] + 1}
    if current["revision"] == 0:
        db.add(ContentBlock(id=KEY, payload=value, updated_by=actor, updated_at=now))
    else:
        count = db.execute(
            update(ContentBlock)
            .where(
                ContentBlock.id == KEY,
                ContentBlock.payload["revision"].as_integer() == current["revision"],
            )
            .values(payload=value, updated_by=actor, updated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if count != 1:
            db.rollback()
            external.fail(409, "revision_conflict")
    db.add(
        ContentBlock(
            id=external.AUDIT + uuid4().hex,
            updated_by=actor,
            updated_at=now,
            payload={
                "action": "policy_updated",
                "revision": value["revision"],
                "actor": actor,
                "trace_id": trace,
                "timestamp": now.isoformat(),
                "defaultPinned": body["defaultPinned"],
                "forced": body["forced"],
                "hidden": body["hidden"],
            },
        )
    )
    db.commit()
    db.expire_all()
    return value
