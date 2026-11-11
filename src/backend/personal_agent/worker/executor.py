"""Workers on the core chat runtime: slots, launch, delivery and settlement (SUPERAGENT_DESIGN §6).

A queued task is claimed into an Assignment and launched as a core ChatRun on its
Worker session (``activation``). While the Assignment runs, every step:

* steers unread updates into the live run (``replace_latest=False``; the Worker's
  steering hook takes them as one batch at the next reasoning boundary), or wakes an
  idle session with all of them merged into one activation;
* waits while core still has work for the session: a live run, or a background
  command whose completion core delivers itself (``commands/delivery``);
* retries a transient failure with backoff (§6.5), and stops background commands
  that outlive ``BACKGROUND_LIMIT`` (§6.3);
* otherwise settles the task from the Worker's last Report (``settle``).

Core owns streaming, history, steering, cancellation and command wakes; this module
only acts at run boundaries. Assignments are leased, so another process takes over
when this one dies.
"""
import asyncio
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field

from sqlalchemy import select

from core.db.engine import SessionLocal
from core.db.models import ChatMessage, ChatRun
from core.db.models.command import CommandTask
from core.db.threads import run_in_session
from personal_agent import access
from personal_agent.computer import computer_wait
from personal_agent.identity.config import concurrency
from personal_agent.models import PersonalAgent, PersonalAssignment, PersonalWorkItem
from personal_agent.scheduling import proactive_guard, proactive_tasks
from personal_agent.worker import activation, inputs, settle

log = logging.getLogger(__name__)
MAX_ACTIVE_RUNS = 24
CLAIM_INTERVAL_SEC = 0.3
STEP_INTERVAL_SEC = 1.0
# Consecutive failed steps of one Assignment before its task fails instead of retrying every second.
STEP_FAILURES = 5
MAINTENANCE_INTERVAL_SEC = 60
TASK_LEASE_SEC = 90
BACKGROUND_LIMIT = 30 * 60
LIVE_RUNS = ("pending", "running")


@dataclass
class Step:
    """What one step found; decided under the database, acted on outside it."""
    kind: str
    assignment_id: str = ""
    task_id: str = ""
    user_id: str = ""
    chat_id: str = ""
    box: str = ""
    run_id: str = ""
    updates: list = field(default_factory=list)
    artifact_ids: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    reason: str = ""
    goal: str = ""
    text: str = ""
    final: bool = False


def latest_run(db, chat_id):
    return db.scalar(select(ChatRun).where(ChatRun.chat_id == chat_id)
                     .order_by(ChatRun.created_at.desc(), ChatRun.run_id.desc()).limit(1))


def last_reply(db, asg):
    """The Worker's last non-empty reply in this Assignment."""
    rows = db.scalars(select(ChatMessage).where(ChatMessage.chat_id == asg.chat_id, ChatMessage.role == "assistant",
        ChatMessage.created_at >= asg.created_at).order_by(ChatMessage.chat_seq.desc()).limit(20)).all()
    return next((row.content for row in rows if (row.content or "").strip()), "")


def commands_of(db, chat_id):
    return [{"task_id": row.task_id, "command": row.command, "status": row.status, "notice": row.notice_status}
            for row in db.query(CommandTask).filter(CommandTask.chat_id == chat_id).all()]


def steer_id(message_id, run_id):
    # Stable per (input, run): a repeated step is idempotent, a later run gets the input again.
    return (message_id + ":" + run_id)[:64]


def _approved_payloads(db, ident):
    row = db.get(PersonalWorkItem, ident)
    return (row.id, row.run_id, row.owner), dict((row.checkpoint or {}).get("approved_action_payloads", {}))


def _consume_payload(db, ident, digest):
    """Drop one dispatched action; False when the task now waits for the user."""
    row = db.get(PersonalWorkItem, ident)
    state = dict(row.checkpoint or {})
    remaining = dict(state.get("approved_action_payloads", {})); remaining.pop(digest, None)
    question = state.get("pending_question") or {}
    if question and state.get("task_wait_reason") == (question.get("text") or "")[:300] and not state.get("pending_approval"):
        # The question was asked while this action waited for approval. The action has now run, so the task
        # runs again and says what happened; it asks again if the question still stands.
        state.pop("task_wait_reason", None); state.pop("pending_question", None)
    state["approved_action_payloads"] = remaining; row.checkpoint = state; db.commit()
    return not (state.get("pending_approval") or state.get("task_wait_reason"))


async def dispatch_approved(ident, uid):
    """Run the exact actions the user approved, before the model runs again."""
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
                [item["path"] for item in action.get("attachments", [])], action.get("reply_to_uid", ""))
        elif action["kind"] == "hugagent.session":
            from personal_agent.sessions.session_manager import dispatch_approved as dispatch_session
            await dispatch_session(ident, uid, action)
        elif action["kind"] == "computer.action":
            await functions["pa_computer_action"](action["action"], action["target"])
        if not await run_in_session(_consume_payload, ident, digest): break


class Executor:
    def __init__(self, start=None):
        # ``start(accepted)`` launches an accepted run; tests replace it.
        self.start_run = start or activation.start
        self.loop_task = None
        self.background = None
        self.busy = {}
        self.failures = {}
        self.running = 0
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
        # Core runs keep going; another process takes the Assignments over when their leases end.
        self.stopping = True
        if self.loop_task:
            self.loop_task.cancel()
            with suppress(asyncio.CancelledError): await self.loop_task
        if self.background:
            await self.background.stop()
        for task in list(self.busy.values()): task.cancel()
        await asyncio.gather(*self.busy.values(), return_exceptions=True)
        self.busy.clear()

    # -- slots -----------------------------------------------------------------

    def claim(self):
        """Bind the oldest queued task of an owner with a free slot to a new Assignment.

        A task the user follows up on (it ran before and has unread input) goes first,
        and may take one extra slot when the owner's slots are full (§6.4)."""
        with SessionLocal() as db:
            waiting = select(PersonalWorkItem.agent_id).where(PersonalWorkItem.status == "queued").distinct()
            agents = db.scalars(select(PersonalAgent).where(PersonalAgent.status == "ready", PersonalAgent.id.in_(waiting)).order_by(PersonalAgent.created_at)).all()
            for agent in agents:
                # Queued work waits while the owner's Personal Agent permission is revoked.
                if not access.allowed(db, agent.user_id): continue
                # Lock the owner while counting/claiming: limits span all workers.
                db.refresh(agent, with_for_update=True)
                if agent.status != "ready": continue
                live = db.scalars(select(PersonalAssignment.slot).where(PersonalAssignment.agent_id == agent.id,
                    PersonalAssignment.status == "running", PersonalAssignment.lease_until > time.time())).all()
                used, extra = sum(slot == "task" for slot in live), "follow_up" in live
                limit = concurrency(agent.config)
                queued = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id,
                    PersonalWorkItem.status == "queued").order_by(PersonalWorkItem.created_at).limit(50)).all()
                ran = set(db.scalars(select(PersonalAssignment.work_item_id).where(
                    PersonalAssignment.work_item_id.in_([row.id for row in queued]))).all())
                follow_ups = {row.id for row in queued if row.id in ran and inputs.unread(db, row)}
                from personal_agent.scheduling.schedules import is_proactive
                # Follow-ups first; a proactive task, which nobody waits for, after the user's own tasks.
                for row in sorted(queued, key=lambda row: (row.id not in follow_ups, is_proactive(row))):
                    if used < limit: slot = "task"
                    elif row.id in follow_ups and not extra: slot = "follow_up"
                    else: continue
                    row = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == row.id,
                        PersonalWorkItem.status == "queued").with_for_update(skip_locked=True))
                    if row is None: continue
                    from personal_agent.tasks.task_threads import sync_thread
                    session = sync_thread(db, row, agent.user_id)
                    asg = PersonalAssignment(id="pa_asg_" + uuid.uuid4().hex, work_item_id=row.id, agent_id=agent.id,
                        chat_id=session.chat_id, status="running", slot=slot, owner=self.identity,
                        lease_until=time.time() + TASK_LEASE_SEC)
                    db.add(asg)
                    row.status, row.owner, row.run_id, row.lease_until = "running", self.identity, asg.id, asg.lease_until
                    db.commit()
                    return asg.id
                db.commit()
        return None

    # -- launch ----------------------------------------------------------------

    def _begin(self, asg_id):
        with SessionLocal() as db:
            asg = db.get(PersonalAssignment, asg_id)
            task = db.get(PersonalWorkItem, asg.work_item_id) if asg else None
            if not asg or asg.owner != self.identity or task.status != "running" or task.run_id != asg.id:
                return None
            return task.id, db.get(PersonalAgent, task.agent_id).user_id

    def _ready(self, asg_id):
        """The task may run now; otherwise it waits for the user or the computer and the Assignment ends."""
        from personal_agent.tasks.worker import blocked, wait_for_user
        with SessionLocal() as db:
            asg = db.get(PersonalAssignment, asg_id)
            row = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == asg.work_item_id).with_for_update())
            if asg.owner != self.identity: return False
            if row.run_id != asg.id or row.status != "running":
                # Cancelled or paused while approved actions were dispatched.
                settle.stopped(asg)
                db.commit()
                return False
            row.pop_state("decided_during_run")
            reason = blocked(row)
            parked = computer_wait.park(row)
            if reason: wait_for_user(db, row, reason)
            if reason or parked: settle.end(asg, "not_ready")
            db.commit()
            return not (reason or parked)

    def _guard(self, task_id):
        """Before a proactive task runs, keep a copy of the files it must leave as they are (proactive_guard)."""
        from personal_agent.scheduling.schedules import is_proactive
        with SessionLocal() as db:
            task = db.get(PersonalWorkItem, task_id)
            if is_proactive(task): proactive_guard.take(task.agent_id, task.id)

    async def launch(self, asg_id):
        try:
            started = await asyncio.to_thread(self._begin, asg_id)
            if started is None: return
            task_id, uid = started
            await dispatch_approved(task_id, uid)
            if not await asyncio.to_thread(self._ready, asg_id): return
            await asyncio.to_thread(self._guard, task_id)
            await self.activate(asg_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from core.runs.tool_effects.errors import find_tool_outcome_unknown
            log.warning("Personal task launch failed: %s (%s)", asg_id, type(exc).__name__, exc_info=True)
            await asyncio.to_thread(self._settle, asg_id, self._fail, str(exc), unknown=find_tool_outcome_unknown(exc) is not None)

    async def activate(self, asg_id, note=None):
        """Admit and start one activation; a run that became live meanwhile gets the input as a steer."""
        from core.chat.sequencer import ChatBusyError
        from personal_agent.tasks.attachments import stage_inputs
        act = await run_in_session(activation.plan, asg_id, self.identity, note)
        if act is None: return
        act.attachments = await stage_inputs(act.task_id, act.user_id, act.box, act.artifact_ids)
        try:
            accepted = await run_in_session(activation.accept, act, self.identity)
        except ChatBusyError:
            return
        if accepted is not None:
            await self.start_run(accepted)

    # -- steps -----------------------------------------------------------------

    def observe(self, asg_id):
        with SessionLocal() as db:
            asg = db.scalar(select(PersonalAssignment).where(PersonalAssignment.id == asg_id).with_for_update())
            if asg is None or asg.status != "running" or asg.owner != self.identity:
                return Step("gone")
            task = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == asg.work_item_id).with_for_update())
            agent = db.get(PersonalAgent, task.agent_id)
            base = dict(assignment_id=asg.id, task_id=task.id, user_id=agent.user_id, chat_id=asg.chat_id,
                        box=agent.sandbox_session_id, goal=task.goal)
            run = latest_run(db, asg.chat_id)
            live = run if run is not None and run.status in LIVE_RUNS else None
            if task.status == "running" and task.run_id != asg.id:
                # Another Assignment runs the task now and owns whatever runs on its session.
                return Step("stop", **base)
            from personal_agent.scheduling.schedules import is_proactive
            if task.status == "running" and is_proactive(task) and proactive_tasks.revoked(db, agent):
                # Nobody asked for this task, and the owner may no longer have the agent work: stop it now,
                # not at the end of its run. An ordinary task finishes the run the user started.
                proactive_tasks.cancel(db, task, proactive_tasks.REVOKED)
                db.commit()
                proactive_tasks.settled(agent.id, "task_failed")
            if task.status != "running" or task.run_id != asg.id:
                return Step("stop", run_id=live.run_id if live else "",
                            commands=[c for c in commands_of(db, asg.chat_id) if c["status"] in settle.LIVE_COMMANDS], **base)
            now = time.time()
            asg.lease_until = task.lease_until = now + TASK_LEASE_SEC
            if run is not None and run.status in ("failed", "cancelled", "needs_attention"):
                # Input steered into a run that died before its boundary is delivered again.
                lost = settle.undelivered_steers(db, run.run_id)
                if lost: inputs.unconsume(task, lost)
            updates = inputs.unread(db, task)[:inputs.BATCH]
            if live:
                asg.run_id, asg.idle_since = live.run_id, None
                db.commit()
                return Step("steer" if updates else "wait", run_id=live.run_id, updates=updates,
                            artifact_ids=inputs.new_attachments(task), **base)
            # Runs of this Assignment: its activations, core's handoffs and command wakes.
            # A failure is handled before new input, which the retry then carries.
            ours = run is not None and run.created_at >= asg.created_at
            if ours and run.status in ("failed", "needs_attention"):
                unknown = run.status == "needs_attention"
                reason = run.failure_reason or ""
                if not unknown and settle.transient(reason):
                    if asg.retry_at is None and not settle.schedule_retry(asg):
                        db.commit()
                        return Step("fail", reason=reason, **base)
                    if asg.retry_at is not None and asg.retry_at <= now:
                        db.commit()
                        return Step("retry", reason=reason[:200], **base)
                    db.commit()
                    return Step("wait", **base)
                db.commit()
                return Step("needs_attention" if unknown else "fail", reason=reason, **base)
            if ours and run.status == "cancelled":
                db.commit()
                return Step("cancelled", **base)
            if updates:
                db.commit()
                return Step("wake", **base)
            commands = commands_of(db, asg.chat_id)
            if any(c["notice"] in ("pending", "reserved") for c in commands):
                # Core wakes the session with the completion (commands/delivery.prepare).
                db.commit()
                return Step("wait", **base)
            running = [c for c in commands if c["status"] in settle.LIVE_COMMANDS]
            if running and not settle.hands_over(task, asg):
                asg.idle_since = asg.idle_since or now
                over = now - asg.idle_since >= BACKGROUND_LIMIT
                db.commit()
                return Step("budget" if over else "wait", commands=running, **base)
            report = settle.report_of(task, asg).get("status")
            if not ours and not report:
                # Only an earlier Assignment's run is there (a crash before this one's
                # activation, or a wake it adopted): this Assignment has not run yet.
                db.commit()
                return Step("wake", **base)
            text = "" if report in ("ready", "dropped") else last_reply(db, asg)
            db.commit()
            return Step("settle", text=text, final=report in settle.HANDOVER | {"waiting"}, **base)

    def _settle(self, asg_id, action, *args, **kwargs):
        with SessionLocal() as db:
            asg = db.scalar(select(PersonalAssignment).where(PersonalAssignment.id == asg_id).with_for_update())
            if asg is None or asg.status != "running" or asg.owner != self.identity: return
            task = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == asg.work_item_id).with_for_update())
            # A run of the task is over: what its command line changed in the persona and the memory is put back.
            proactive_guard.close(db, task)
            if task.status != "running" or task.run_id != asg.id:
                self._tell_guard(db, task)
                settle.stopped(asg)
            else:
                action(db, asg, task, *args, **kwargs)
            db.commit()
            self._release_guard(task)

    def _tell_guard(self, db, task):
        # The task was stopped or has failed: no summary will say that files were put back.
        if task.status in ("failed", "cancelled"): proactive_guard.tell(db, task)

    def _release_guard(self, task):
        from personal_agent.identity.config import TERMINAL
        if task.status in TERMINAL: proactive_guard.discard(task.agent_id, task.id)

    def _finish(self, db, asg, task, fallback):
        if proactive_tasks.fails_quietly(task, asg, fallback):
            return proactive_tasks.fail_quietly(db, asg, task)
        uid = db.get(PersonalAgent, task.agent_id).user_id
        settle.finished(db, asg, task, uid, self.identity, proactive_guard.noted(task, fallback))

    def _fail(self, db, asg, task, reason, unknown=False):
        """``settle.failed``; a proactive task that nothing holds for the user fails without a notice."""
        if not unknown and proactive_tasks.fails_quietly(task, asg):
            return proactive_tasks.fail_quietly(db, asg, task, reason)
        settle.failed(db, asg, task, reason, unknown=unknown)

    def _halt(self, db, asg, task, action, *args):
        """``settle.cancelled`` or ``settle.over_budget``; a proactive task they would pause with a notice fails quietly."""
        if proactive_tasks.gives_up(task, resumes=action is settle.cancelled):
            return proactive_tasks.fail_quietly(db, asg, task, proactive_tasks.INTERRUPTED)
        action(db, asg, task, *args)

    def _end(self, asg_id, reason):
        with SessionLocal() as db:
            asg = db.get(PersonalAssignment, asg_id)
            if asg is not None and asg.status == "running" and asg.owner == self.identity:
                task = db.get(PersonalWorkItem, asg.work_item_id)
                proactive_guard.close(db, task)
                self._tell_guard(db, task)
                settle.end(asg, reason)
                db.commit()
                self._release_guard(task)

    async def step(self, asg_id):
        try:
            await self._step(asg_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A step that keeps failing (an attachment that can no longer be staged) must not
            # hold the slot and renew the lease forever: the task fails with the reason.
            self.failures[asg_id] = self.failures.get(asg_id, 0) + 1
            if self.failures[asg_id] < STEP_FAILURES: raise
            self.failures.pop(asg_id, None)
            log.warning("Personal assignment step keeps failing: %s (%s)", asg_id, type(exc).__name__)
            await asyncio.to_thread(self._settle, asg_id, self._fail, f"{type(exc).__name__}: {exc}"[:300])
        else:
            self.failures.pop(asg_id, None)

    async def _step(self, asg_id):
        s = await asyncio.to_thread(self.observe, asg_id)
        if s.kind in ("gone", "wait"):
            return
        if s.kind == "stop":
            await self.halt(s)
            await asyncio.to_thread(self._end, asg_id, "stopped")
        elif s.kind == "steer":
            await self.steer(s)
        elif s.kind == "wake":
            await self.activate(asg_id)
        elif s.kind == "retry":
            await self.activate(asg_id, activation.RETRY.format(reason=s.reason))
        elif s.kind == "fail":
            await asyncio.to_thread(self._settle, asg_id, self._fail, s.reason)
        elif s.kind == "needs_attention":
            await asyncio.to_thread(self._settle, asg_id, self._fail, s.reason, unknown=True)
        elif s.kind == "cancelled":
            await asyncio.to_thread(self._settle, asg_id, self._halt, settle.cancelled)
        elif s.kind == "budget":
            await self.cancel_commands(s)
            await asyncio.to_thread(self._settle, asg_id, self._halt, settle.over_budget, s.commands)
        elif s.kind == "settle":
            fallback = ""
            if not s.final and s.text.strip():
                from personal_agent.tasks.runtime import finalize
                fallback = await finalize(s.goal, s.text, await self.provider(s.task_id))
            await asyncio.to_thread(self._settle, asg_id, self._finish, fallback)

    async def provider(self, task_id):
        def _provider(db):
            task = db.get(PersonalWorkItem, task_id)
            return (db.get(PersonalAgent, task.agent_id).config or {}).get("model_provider_id")
        return await run_in_session(_provider)

    async def steer(self, s):
        """``steer`` delivery: every unread update goes into the live run, consumed together at its next boundary."""
        from core.chat.steer import put_pending_steer
        from core.chat.steer_queue import SteerQueueConflict
        from personal_agent.tasks.attachments import stage_inputs
        attachments = await stage_inputs(s.task_id, s.user_id, s.box, s.artifact_ids) if s.artifact_ids else ""
        delivered = []
        for index, (message_id, text) in enumerate(s.updates):
            if index == len(s.updates) - 1 and attachments:
                text += attachments
            try:
                await put_pending_steer(s.run_id, {"chat_id": s.chat_id, "user_id": s.user_id, "message": text[:10000],
                    "steer_id": steer_id(message_id, s.run_id), "replace_latest": False})
            except SteerQueueConflict:
                # The run ended meanwhile: the rest arrive as the next activation.
                break
            delivered.append(message_id)

        def _consume(db):
            task = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == s.task_id).with_for_update())
            if task.run_id != s.assignment_id: return
            inputs.consume(task, delivered)
            if len(delivered) == len(s.updates):
                inputs.staged(task, s.artifact_ids)
            db.commit()
        if delivered:
            await run_in_session(_consume)

    async def halt(self, s):
        """The task was stopped by the user: cancel its live run and its commands."""
        if s.run_id:
            from core.runs.runtime import cancel_run
            with suppress(Exception):
                await cancel_run(s.run_id, user_id=s.user_id)
        await self.cancel_commands(s)

    async def cancel_commands(self, s):
        from core.llm.ports import command_execution
        for command in s.commands:
            try:
                await command_execution().cancel(command["task_id"], s.user_id, s.box)
            except Exception as exc:
                log.warning("Personal task command stop failed (%s)", type(exc).__name__)

    # -- ownership -------------------------------------------------------------

    def owned(self):
        with SessionLocal() as db:
            return db.scalars(select(PersonalAssignment.id).where(PersonalAssignment.status == "running",
                PersonalAssignment.owner == self.identity)).all()

    def recover(self):
        """Take over Assignments whose process is gone (§6.5).

        A live or completed run is simply watched from here on. Otherwise the run died
        with its process: the Assignment ends and the task restarts with a recovery note,
        or waits for the user when an external effect may have happened."""
        from personal_agent.tasks import worker as states
        with SessionLocal() as db:
            now = time.time()
            # Fence recovery against other recoverers and claimers until the commit below.
            rows = db.scalars(select(PersonalAssignment).where(PersonalAssignment.status == "running",
                PersonalAssignment.lease_until < now).with_for_update(skip_locked=True)).all()
            for asg in rows:
                task = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == asg.work_item_id).with_for_update())
                if task.status != "running" or task.run_id != asg.id:
                    settle.stopped(asg)
                    continue
                run = latest_run(db, asg.chat_id)
                if run is not None and run.status == "needs_attention" and run.created_at >= asg.created_at:
                    # Same outcome as the step path: an unknown tool effect waits for the user.
                    settle.failed(db, asg, task, run.failure_reason or "", unknown=True)
                    continue
                if run is not None and (run.status in LIVE_RUNS
                                        or (run.status == "completed" and run.created_at >= asg.created_at)):
                    asg.owner = task.owner = self.identity
                    asg.lease_until = task.lease_until = now + TASK_LEASE_SEC
                    continue
                settle.restarted(db, asg, task)
            # Tasks a previous version of this module ran have no Assignment.
            active = select(PersonalAssignment.work_item_id).where(PersonalAssignment.status == "running")
            for task in db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.status == "running",
                    PersonalWorkItem.lease_until < now, PersonalWorkItem.id.notin_(active)).with_for_update(skip_locked=True)).all():
                # An interrupted model run can have unknown external effects.
                states.interrupt(db, task, "执行进程中断。请检查已执行的操作后继续。")
            db.commit()

    async def loop(self):
        from personal_agent.health import HealthEntry
        health = HealthEntry("worker", self.identity, CLAIM_INTERVAL_SEC)
        await asyncio.to_thread(health.register)
        last_maintenance = 0
        last_computer_check = 0
        last_step = 0
        while not self.stopping:
            if health.beat_due: await asyncio.to_thread(health.beat)
            for ident, task in list(self.busy.items()):
                if task.done():
                    with suppress(asyncio.CancelledError):
                        if task.exception(): log.warning("Personal assignment step failed: %s (%s)", ident, type(task.exception()).__name__)
                    self.busy.pop(ident, None)
            try:
                if time.monotonic() - last_computer_check >= 3:
                    await computer_wait.resume_available()
                    last_computer_check = time.monotonic()
                if time.monotonic() - last_maintenance > MAINTENANCE_INTERVAL_SEC:
                    await asyncio.to_thread(self.recover)
                    last_maintenance = time.monotonic()
                if time.monotonic() - last_step >= STEP_INTERVAL_SEC:
                    last_step = time.monotonic()
                    owned = await asyncio.to_thread(self.owned)
                    self.running = len(owned)
                    for ident in owned:
                        if ident not in self.busy:
                            self.busy[ident] = asyncio.create_task(self.step(ident))
                if self.running < MAX_ACTIVE_RUNS:
                    ident = await asyncio.to_thread(self.claim)
                    if ident:
                        self.running += 1
                        self.busy[ident] = asyncio.create_task(self.launch(ident))
                        continue
                if health.recovering: await asyncio.to_thread(health.ok)
            except Exception as exc:
                log.warning("Personal worker cycle failed (%s)", type(exc).__name__)
                await asyncio.to_thread(health.error, f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(CLAIM_INTERVAL_SEC)


executor = Executor()

async def start_worker(): await executor.start()
async def stop_worker(): await executor.stop()
