"""Preserve the exact validated UI snapshot before an explicit schema reset."""

from copy import deepcopy
from hashlib import sha256
from core.db.models import ContentBlock
from core.infra.time import utc_now

PREFIX = "workbench_state_recovery:"


def preserve_before_reset(db, state_row):
    value = state_row.payload
    fingerprint = sha256(
        (state_row.id + ":" + str(value["schemaVersion"]) + ":" + str(value["revision"])).encode()
    ).hexdigest()
    identifier = PREFIX + fingerprint
    if db.get(ContentBlock, identifier) is None:
        db.add(
            ContentBlock(
                id=identifier,
                payload={
                    "stateRowId": state_row.id,
                    "snapshot": deepcopy(value),
                },
                updated_by=state_row.updated_by,
                updated_at=utc_now(),
            )
        )
    return identifier
