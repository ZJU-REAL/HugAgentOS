"""Bounded revocation reconciliation, independent of browser notification reads."""

import logging

from sqlalchemy import select, union
from starlette.concurrency import run_in_threadpool

from core.applications.revocation import EXTERNAL_PREFIXES, stage_unavailable, _close_workers
from core.db.engine import SessionLocal
from core.db.models import ContentBlock
from core.db.models.plugin_resource import PluginResource


def owners(after, limit):
    def records(prefix, column):
        return select(column.label("owner")).where(
            ContentBlock.id.startswith(prefix, autoescape=True)
        )

    candidates = union(
        select(PluginResource.user_id.label("owner")).where(
            PluginResource.status.in_(["active", "starting", "warming", "warm", "closing"])
        ),
        records("workbench_activity:", ContentBlock.updated_by),
        records("workbench_approval:", ContentBlock.payload["userId"].as_string()),
        records("workbench_plugin_lease:", ContentBlock.payload["userId"].as_string()),
        *(
            records(prefix, ContentBlock.payload["subject"].as_string())
            for prefix in EXTERNAL_PREFIXES
        ),
    ).subquery()
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(candidates.c.owner)
                .where(candidates.c.owner.is_not(None), candidates.c.owner > after)
                .order_by(candidates.c.owner)
                .limit(limit)
            )
        )


def stage(user_id):
    # This session is created, used and closed in a single worker thread.
    with SessionLocal() as db:
        return stage_unavailable(db, user_id)


async def reconcile_owner(user_id):
    resource_ids = await run_in_threadpool(stage, user_id)
    with SessionLocal() as db:
        rows = (
            db.query(PluginResource)
            .filter(
                PluginResource.user_id == user_id,
                PluginResource.resource_id.in_(resource_ids),
            )
            .order_by(PluginResource.resource_id)
            .all()
        )
        return await _close_workers(db, rows)


async def sweep(after="", limit=100):
    batch = await run_in_threadpool(owners, after, limit)
    for user_id in batch:
        try:
            await reconcile_owner(user_id)
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Workbench owner reconciliation deferred: %s", type(error).__name__
            )
    return batch[-1] if len(batch) == limit else ""
