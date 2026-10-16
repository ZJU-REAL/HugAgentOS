"""Current-user database observations; registration is not process liveness."""

import time

from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.exc import NoSuchTableError


def registered(db, user_id):
    try:
        columns = {c["name"] for c in inspect(db.get_bind()).get_columns("plugin_resources")}
    except NoSuchTableError:
        columns = set()
    if not {"application_key", "instance_id"} <= columns:
        return {
            "available": False,
            "reason": "awaiting_application_owner_schema",
            "states": None,
            "validLeases": None,
        }
    table = Table("plugin_resources", MetaData(), autoload_with=db.get_bind())
    own = (table.c.user_id == user_id, table.c.application_key.is_not(None))
    states = {
        status: count
        for status, count in db.execute(
            select(table.c.status, func.count()).where(*own).group_by(table.c.status)
        )
    }
    valid = db.scalar(
        select(func.count()).where(
            *own,
            table.c.expires_at > time.time(),
            table.c.status.in_(("active", "starting", "warming", "warm", "closing")),
        )
    )
    return {"available": True, "states": states, "validLeases": valid}


def snapshot(db, user_id):
    return {
        "registration": registered(db, user_id),
        "runtime": {
            "complete": False,
            "provider": None,
            "appOwnerRunningWorkers": None,
            "appOwnerStartingWorkers": None,
            "reason": "enumeration_not_supported",
        },
    }
