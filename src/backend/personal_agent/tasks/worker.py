"""Database-owned background work, independent of browsers and HTTP requests."""
import asyncio
import logging
import time
import uuid
from contextlib import suppress
from sqlalchemy import select
from core.db.engine import SessionLocal
from core.runs.journal import durable_run_binding
from personal_agent import access
from personal_agent.computer import computer_wait
from personal_agent.models import PersonalAgent, PersonalWorkItem
from personal_agent.context import AgentContext
from personal_agent.identity.config import concurrency
from core.db.threads import run_in_session

log = logging.getLogger(__name__)
MAX_AUTO_RESUMES = 2
MAX_ACTIVE_RUNS = 24
CLAIM_INTERVAL_SEC = 0.3
SCHEDULE_INTERVAL_SEC = 15
CHANNEL_INTERVAL_SEC = 3
SESSION_WATCH_INTERVAL_SEC = 10
MAINTENANCE_INTERVAL_SEC = 60
HEARTBEAT_INTERVAL_SEC = 60
HEARTBEAT_CLEANUP_SEC = 600
TASK_LEASE_SEC = 90
UNCONFIRMED = "操作结果尚未确认，请核对后继续。"
AWAITING_APPROVAL = "等待你批准操作"

def resumable(row):
    """An interrupted run can restart only when no external effect is unconfirmed."""
    state = row.checkpoint or {}
    return not (row.unconfirmed() or state.get("pending_approval") or state.get("approved_action_payloads")
        or state.get("task_wait_reason")) and state.get("auto_resumes", 0) < MAX_AUTO_RESUMES

def blocked(row):
    """Why the task must wait for the user before (or instead of) finishing, or None."""
    state = row.checkpoint or {}
    if row.unconfirmed(): return UNCONFIRMED
    if state.get("task_wait_reason"): return state["task_wait_reason"]
    if state.get("pending_approval"): return AWAITING_APPROVAL
    return None

def pause(db, row, reason):
    row.pause(reason)
    notify(db, row, row.label + "已暂停：" + reason)

def wait_for_user(db, row, reason):
    row.pause(reason)
    if reason == AWAITING_APPROVAL: approval_notice(db, row)

def interrupt(db, row, reason):
    """Requeue a safe interrupted task. Otherwise ask the user to check it."""
    if computer_wait.park(row): return
    if resumable(row):
        row.checkpoint = {**(row.checkpoint or {}), "auto_resumes": (row.checkpoint or {}).get("auto_resumes", 0) + 1}
        row.requeue()
        return
    pause(db, row, reason)

def approval_notice(db, row):
    """An IM user cannot approve in IM. Tell that conversation where to approve."""
    from personal_agent.channels.channel_bridge import origin_of, notify_soon
    notify_soon(origin_of(db, task_id=row.id), row.label + "需要你批准一个操作。请打开 Personal Agent 网页，在“需要你处理”中批准或拒绝。")

def notify(db, row, text):
    """Post a short status notice in the main conversation. It is not a task result."""
    agent = db.get(PersonalAgent, row.agent_id)
    try:
        AgentContext(db, agent.user_id).conversation.append(agent, "assistant", text, row.id, relation="status")
    except Exception as exc:
        log.warning("Personal notice failed (%s)", type(exc).__name__)

def _renew_lease(db, ident, identity):
    row = db.get(PersonalWorkItem, ident)
    if row is None or row.status != "running" or row.owner != identity or not row.lease_until or row.lease_until < time.time():
        return False
    row.lease_until = time.time() + TASK_LEASE_SEC
    db.commit()
    return True

def _start_run(db, ident, identity):
    row = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == ident).with_for_update())
    if row is None or row.owner != identity or row.status != "running": return None
    agent = db.get(PersonalAgent, row.agent_id)
    from personal_agent.tasks.task_threads import sync_thread
    session = sync_thread(db, row, agent.user_id)
    started = agent.user_id, session.chat_id, row.run_id
    db.commit()
    return started

def _ready_to_execute(db, ident, identity, run_id):
    row = db.get(PersonalWorkItem, ident)
    if row is None or row.owner != identity or row.run_id != run_id or row.status != "running" or not row.lease_until or row.lease_until < time.time(): return False
    row.pop_state("decided_during_run")
    reason = blocked(row)
    parked = computer_wait.park(row)
    if reason: wait_for_user(db, row, reason)
    db.commit()
    return not (reason or parked)

def _record_outcome(db, ident, identity, uid, result, checkpoint):
    # Locked until the outcome is written, so an approval cannot slip in between.
    row = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == ident).with_for_update())
    if row.owner != identity or row.status != "running": return
    state = row.checkpoint or {}
    if computer_wait.park(row):
        db.commit()
    elif row.unconfirmed() or state.get("task_wait_reason"):
        wait_for_user(db, row, blocked(row))
        db.commit()
    elif row.pop_state("decided_during_run"):
        # The user answered while this run was ending. Run again to act on the answer.
        row.requeue()
        db.commit()
    elif state.get("pending_approval"):
        wait_for_user(db, row, AWAITING_APPROVAL)
        db.commit()
    else:
        AgentContext(db, uid).tasks.complete(ident, result, {**(row.checkpoint or {}), **checkpoint}, owner=identity)

def _record_failure(db, ident, identity, unknown):
    row = db.get(PersonalWorkItem, ident)
    if not (row and row.status == "running" and row.owner == identity): return
    if not unknown and computer_wait.park(row):
        db.commit()
        return
    uncertain = unknown or blocked(row) is not None
    row.status = "waiting" if uncertain else "failed"
    row.waiting_reason = "操作结果未知，需核对后继续" if uncertain else "任务执行失败。请检查模型、连接器或电脑状态后重试。"
    notify(db, row, row.label + ("需要你核对操作结果。" if uncertain else "执行失败。可以在任务卡片中重试。"))
    if row.status == "failed":
        from personal_agent.workspace.file_memory import record_task
        record_task(db, row, db.get(PersonalAgent, row.agent_id))
    db.commit()

def _approved_payloads(db, ident):
    row = db.get(PersonalWorkItem, ident)
    return (row.id, row.run_id, row.owner), dict((row.checkpoint or {}).get("approved_action_payloads", {}))

def _consume_payload(db, ident, digest):
    """Drop one dispatched action; False when the task now waits for the user."""
    row = db.get(PersonalWorkItem, ident)
    state = dict(row.checkpoint or {})
    remaining = dict(state.get("approved_action_payloads", {})); remaining.pop(digest, None)
    state["approved_action_payloads"] = remaining; row.checkpoint = state; db.commit()
    return not (state.get("pending_approval") or state.get("task_wait_reason"))

class PersonalAgentWorker:
    def __init__(self, execute=None):
        self.execute = execute
        self.loop_task = None
        self.background = None
        self.active = {}
        self.stopping = False
        self.identity = "pa-worker-" + uuid.uuid4().hex

    async def start(self):
        if self.loop_task and not self.loop_task.done(): return
        self.stopping = False
        self.loop_task = asyncio.create_task(self.loop())
        from personal_agent.background import BackgroundServices
        self.background = BackgroundServices(self.identity)
        self.background.start()

    async def stop(self):
        self.stopping = True
        if self.loop_task:
            self.loop_task.cancel()
            with suppress(asyncio.CancelledError): await self.loop_task
        if self.background:
            await self.background.stop()
        for task in list(self.active.values()): task.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)
        self.active.clear()

    def claim(self):
        with SessionLocal() as db:
            # Scan only owners with queued work. Do not lock every agent on each poll.
            waiting = select(PersonalWorkItem.agent_id).where(PersonalWorkItem.status == "queued").distinct()
            agents = db.scalars(select(PersonalAgent).where(PersonalAgent.status == "ready", PersonalAgent.id.in_(waiting)).order_by(PersonalAgent.created_at)).all()
            for agent in agents:
                # Queued work waits while the owner's Personal Agent permission is revoked.
                if not access.allowed(db, agent.user_id): continue
                # Lock the owner while counting/claiming: limits span all workers.
                db.refresh(agent, with_for_update=True)
                if agent.status != "ready": continue
                live = db.scalars(select(PersonalWorkItem.id).where(PersonalWorkItem.agent_id == agent.id, PersonalWorkItem.status == "running", PersonalWorkItem.lease_until > time.time())).all()
                limit = concurrency(agent.config)
                if len(live) >= limit: continue
                row = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id, PersonalWorkItem.status == "queued").order_by(PersonalWorkItem.created_at).with_for_update(skip_locked=True).limit(1))
                if row is None: continue
                row.status, row.owner = "running", self.identity
                row.lease_until = time.time() + TASK_LEASE_SEC
                row.run_id = "pa_run_" + uuid.uuid4().hex
                db.commit()
                return row.id
        return None

    def recover(self):
        with SessionLocal() as db:
            # Fence recovery against other recoverers and claimers. The lock
            # lasts through interrupt/checkpoint updates and the commit below.
            rows = db.scalars(
                select(PersonalWorkItem)
                .where(PersonalWorkItem.status == "running", PersonalWorkItem.lease_until < time.time())
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                # An interrupted model run can have unknown external effects.
                interrupt(db, row, "执行进程中断。请检查已执行的操作后继续。")
            db.commit()

    async def heartbeat(self, ident):
        while True:
            await asyncio.sleep(10)
            if not await run_in_session(_renew_lease, ident, self.identity):
                running = self.active.get(ident)
                if running: running.cancel()
                return

    async def run(self, ident):
        beat = asyncio.create_task(self.heartbeat(ident))
        try:
            started = await run_in_session(_start_run, ident, self.identity)
            if started is None: return
            uid, chat_id, run_id = started
            async with durable_run_binding(user_id=uid, chat_id=chat_id, kind="personal_agent_task", external_id=ident, binding_run_id=run_id) as binding:
                await self.dispatch_approved(ident, uid)
                if not await run_in_session(_ready_to_execute, ident, self.identity, run_id): return
                from personal_agent.tasks.runtime import execute_task, RunawayOutput
                try:
                    result, checkpoint = await (self.execute or execute_task)(ident, binding)
                except RunawayOutput as exc:
                    reason = "模型输出重复或过长，已停止。请补充要求后点击重试。"
                    def _park_or_pause(db):
                        row = db.get(PersonalWorkItem, ident)
                        if row.status == "running" and row.owner == self.identity:
                            if not computer_wait.park(row): pause(db, row, reason)
                            db.commit()
                    await run_in_session(_park_or_pause)
                    log.warning("Personal task stopped: %s (%s)", ident, type(exc).__name__)
                    return
                await run_in_session(_record_outcome, ident, self.identity, uid, result, checkpoint)
        except asyncio.CancelledError:
            def _interrupt(db):
                row = db.get(PersonalWorkItem, ident)
                if row and row.status == "running" and row.owner == self.identity:
                    interrupt(db, row, "执行已中断。请检查操作结果后继续。")
                    db.commit()
            await run_in_session(_interrupt)
            raise
        except Exception as exc:
            from core.runs.tool_effects.errors import find_tool_outcome_unknown
            await run_in_session(
                _record_failure, ident, self.identity, find_tool_outcome_unknown(exc) is not None
            )
            log.warning("Personal task failed: %s (%s)", ident, type(exc).__name__)
        finally:
            beat.cancel()
            with suppress(asyncio.CancelledError): await beat

    async def dispatch_approved(self, ident, uid):
        from personal_agent.tasks.execution import current_execution
        execution, payloads = await run_in_session(_approved_payloads, ident)
        current_execution.set(execution)
        if not payloads: return
        tools = []
        from personal_agent.tools.email import register_email_tools
        from personal_agent.computer.computer import register_computer_tools
        register_email_tools(tools, ident, uid)
        register_computer_tools(tools, ident, uid)
        functions = {tool.name: tool._func for tool in tools}
        for digest, action in payloads.items():
            if action["kind"] == "email.send":
                await functions["pa_email_send"](action["to"], action["subject"], action["body"],
                    [item["path"] for item in action.get("attachments", [])])
            elif action["kind"] == "hugagent.session":
                from personal_agent.sessions.session_manager import dispatch_approved
                await dispatch_approved(ident, uid, action)
            elif action["kind"] == "computer.action":
                await functions["pa_computer_action"](action["action"], action["target"])
            if not await run_in_session(_consume_payload, ident, digest): break

    async def loop(self):
        last_maintenance = 0
        last_computer_check = 0
        while not self.stopping:
            for ident, task in list(self.active.items()):
                if task.done():
                    with suppress(asyncio.CancelledError): task.exception()
                    self.active.pop(ident, None)
            try:
                if time.monotonic() - last_computer_check >= 3:
                    await computer_wait.resume_available()
                    last_computer_check = time.monotonic()
                if time.monotonic() - last_maintenance > MAINTENANCE_INTERVAL_SEC:
                    await asyncio.to_thread(self.recover)
                    last_maintenance = time.monotonic()
                if len(self.active) < MAX_ACTIVE_RUNS:
                    ident = await asyncio.to_thread(self.claim)
                    if ident:
                        self.active[ident] = asyncio.create_task(self.run(ident))
                        continue
            except Exception as exc:
                log.warning("Personal worker cycle failed (%s)", type(exc).__name__)
            await asyncio.sleep(CLAIM_INTERVAL_SEC)

worker = PersonalAgentWorker()

async def start_worker(): await worker.start()
async def stop_worker(): await worker.stop()
