"""Bound result delivery without changing the committed business outcome."""

import asyncio

from core.infra.background import spawn
from core.infra.logging import get_logger

logger = get_logger(__name__)
DELIVERY_TIMEOUT_SECONDS = 60


async def deliver_result(
    scheduler, user_id, task_id, task_name, status, text, chat_id=None, metadata=None
):
    stopped = asyncio.Event()

    async def deliver():
        from features.automation.delivery_targets import has_inapp, resolve_delivery_targets

        targets = resolve_delivery_targets(metadata)
        if status != "success" or has_inapp(targets):
            await scheduler._send_notification(user_id, task_id, task_name, status, text, chat_id)
        channels = [t for t in targets if t.get("type") == "channel"]
        if status != "success" or not channels or stopped.is_set():
            return
        from integrations.channels.outbound import deliver_to_conversation

        from .automation_artifacts import load_run_files

        files = await asyncio.to_thread(load_run_files, chat_id) if chat_id else []
        for target in channels:
            # A channel library may swallow cancellation. Do not start another
            # send after the deadline, even if the in-flight send returns late.
            if stopped.is_set():
                return
            channel, conversation = target.get("channel_id"), target.get("conversation_id")
            if not channel or not conversation:
                continue
            body = (f"【{task_name}】\n" if task_name else "") + text
            try:
                ok = await deliver_to_conversation(channel, conversation, body, files=files)
                if not ok:
                    logger.error("automation_delivery_failed", task_id=task_id)
            except Exception:
                logger.error("automation_delivery_failed", task_id=task_id, exc_info=True)

    delivery = spawn(deliver(), name=f"automation.delivery.{task_id}")
    try:
        done, _ = await asyncio.wait({delivery}, timeout=DELIVERY_TIMEOUT_SECONDS)
        if not done:
            stopped.set()
            delivery.cancel()
            logger.error("automation_delivery_timeout", task_id=task_id)
        elif not delivery.cancelled():
            # Delivery errors do not turn a committed success into a failure.
            delivery.result()
    except asyncio.CancelledError:
        stopped.set()
        delivery.cancel()
        raise
    except Exception:
        logger.error("automation_delivery_failed", task_id=task_id, exc_info=True)
