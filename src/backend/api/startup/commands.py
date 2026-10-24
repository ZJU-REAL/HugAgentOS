"""Background command monitoring and durable notification dispatch."""
import asyncio
import logging

from core.sandbox import get_sandbox_provider
from core.sandbox.commands.notifications import dispatch
from core.sandbox.commands.runtime import runtime

logger = logging.getLogger(__name__)
_task = None

async def _run():
    while True:
        try:
            await runtime.recover(get_sandbox_provider())
            await dispatch()
        except Exception:
            logger.exception("Background command reconciliation failed")
        await asyncio.sleep(1)

async def startup_commands():
    global _task
    runtime.bind_loop()
    bind = getattr(get_sandbox_provider(), "bind_loop", None)
    if bind is not None:
        bind()
    _task = asyncio.create_task(_run(), name="background-command-reconciliation")

async def shutdown_commands():
    global _task
    if _task is not None:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
        _task = None
    await runtime.shutdown()
