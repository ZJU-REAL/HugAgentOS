"""Deliver command completion without blocking the event loop on database I/O."""

import asyncio
import logging
import uuid

import core.chat.runtime.admission as admission
from core.chat.sequencer import ChatBusyError
from core.sandbox.commands import delivery, store

logger = logging.getLogger(__name__)


async def launch(run_id):
    prepared = await asyncio.to_thread(delivery.load_launch, run_id)
    if prepared is None:
        return
    rows, run, history, args = prepared
    if args is not None:
        prompt = delivery.notice_text(rows)
        await admission.start_run(
            accepted_run=run,
            chat_id=run.chat_id,
            user_id=run.user_id,
            session_messages=history,
            effective_user_message=prompt,
            raw_user_message=prompt,
            context=args["context"],
            model_name=args.get("model_name"),
        )
    from core.sandbox.commands.events import get_command_events

    await get_command_events().publish(run.user_id, run.chat_id, run_id)
    await asyncio.to_thread(delivery.acknowledge, [r["task_id"] for r in rows], run_id)


async def dispatch():
    owner = uuid.uuid4().hex
    after = None
    while True:
        rows = await asyncio.to_thread(store.pending_notices, after=after)
        if not rows:
            return
        await dispatch_batch(rows, owner)
        after = rows[-1]["task_id"]


async def dispatch_batch(batch, owner):
    groups = {}
    for row in batch:
        if row["notice_status"] == "reserved":
            claimed = await asyncio.to_thread(delivery.claim_retry, row["task_id"], owner)
            if claimed:
                try:
                    await launch(claimed)
                except Exception:
                    await asyncio.to_thread(delivery.release_notice, row["task_id"], owner)
                    logger.exception("Command follow-up retained task=%s", row["task_id"])
            continue
        groups.setdefault((row["chat_id"], row["user_id"], row["source_run_id"]), []).append(row)
    for rows in groups.values():
        try:
            run_id = await asyncio.to_thread(delivery.prepare, rows, owner)
            if run_id:
                await launch(run_id)
        except ChatBusyError:
            continue
        except Exception:
            for row in rows:
                await asyncio.to_thread(delivery.release_notice, row["task_id"], owner)
            logger.exception("Command notification retained chat=%s", rows[0]["chat_id"])
