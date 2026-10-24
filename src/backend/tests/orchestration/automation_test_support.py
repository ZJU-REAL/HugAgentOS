"""Exercise the scheduler through durable acceptance, not a private executor."""
import asyncio
from core.db import engine
from features.automation.service import AutomationService


def request(task_id, user_id):
    with engine.SessionLocal() as db:
        service = AutomationService(db)
        service.request_manual_trigger(service.get_task(task_id, user_id))


async def execute_queued(scheduler, task_id, user_id):
    await asyncio.to_thread(request, task_id, user_id)
    before = set(scheduler._executions)
    await scheduler._dispatch_pending()
    await asyncio.gather(*(scheduler._executions - before))
