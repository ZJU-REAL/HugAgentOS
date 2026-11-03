"""Per-run progress telemetry. It only records; nothing reads it to change behaviour.

Keyed by ``work_item_id + run_id``, where ``run_id`` is the Assignment id (the
task's ``run_id`` while it runs), so one row covers every core run of an Assignment.

* :class:`RunTelemetry` is the in-memory state of one Assignment's recording;
* :func:`observe` folds one event ``(event, payload)`` into it;
* :func:`save` writes it to the database;
* :class:`Recorder` ties the three together and never lets a failure reach the caller;
* :func:`hooks` records a core run of the Worker profile from its tool and model hooks.
"""
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import JSON, Column, Integer, String, UniqueConstraint, select

from core.db import engine
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now

log = logging.getLogger(__name__)
RECENT_STEPS = 20
FLUSH_SEC = 5.0


class PersonalAssignmentTelemetry(Base):
    __tablename__ = "personal_assignment_telemetry"
    id = Column(Integer, primary_key=True, autoincrement=True)
    work_item_id = Column(String(64), nullable=False)
    run_id = Column(String(64), nullable=False)
    steps = Column(Integer, nullable=False, default=0)
    tool_counts = Column(JSON, nullable=False, default=dict)
    # Last RECENT_STEPS steps as [tool name, digest of tool name and arguments].
    recent_steps = Column(JSON, nullable=False, default=list)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    last_progress_at = Column(UTCDateTime(timezone=True))
    started_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    __table_args__ = (UniqueConstraint("work_item_id", "run_id", name="uq_pa_telemetry_run"),)


@dataclass
class RunTelemetry:
    work_item_id: str
    run_id: str
    steps: int = 0
    tool_counts: dict = field(default_factory=dict)
    recent_steps: list = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_progress_at: object = None
    started_at: object = field(default_factory=utc_now)


def step_digest(name, args):
    text = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str) if not isinstance(args, str) else args
    return hashlib.sha256((name + "\0" + text).encode()).hexdigest()[:16]


def observe(state, event, payload):
    """Fold one stream event into ``state``. Returns True when the state changed.

    A step is one tool call. It counts as progress unless the same
    tool with the same arguments is already among the recent steps, so a tool
    loop shows as many steps with a stale ``last_progress_at``.
    """
    if event == "tool_call" and isinstance(payload, dict):
        name = str(payload.get("name") or "")
        digest = step_digest(name, payload.get("args"))
        if all(seen != digest for _, seen in state.recent_steps):
            state.last_progress_at = utc_now()
        state.steps += 1
        state.tool_counts[name] = state.tool_counts.get(name, 0) + 1
        state.recent_steps = (state.recent_steps + [[name, digest]])[-RECENT_STEPS:]
        return True
    if event == "context_usage" and isinstance(payload, dict):
        state.prompt_tokens += int(payload.get("prompt_tokens") or 0)
        state.completion_tokens += int(payload.get("completion_tokens") or 0)
        return True
    return False


def save(db, state):
    row = db.scalar(select(PersonalAssignmentTelemetry).where(
        PersonalAssignmentTelemetry.work_item_id == state.work_item_id,
        PersonalAssignmentTelemetry.run_id == state.run_id))
    if row is None:
        row = PersonalAssignmentTelemetry(work_item_id=state.work_item_id, run_id=state.run_id, started_at=state.started_at)
        db.add(row)
    row.steps, row.tool_counts, row.recent_steps = state.steps, dict(state.tool_counts), list(state.recent_steps)
    row.prompt_tokens, row.completion_tokens = state.prompt_tokens, state.completion_tokens
    row.last_progress_at = state.last_progress_at
    db.commit()


class Recorder:
    """Records one run. ``observe`` is cheap; writes happen at most every FLUSH_SEC."""

    def __init__(self, work_item_id, run_id):
        self.state = RunTelemetry(work_item_id, run_id)
        self.dirty = True
        self.flushed = 0.0
        self.loaded = False

    async def observe(self, event, payload):
        try:
            self.dirty = observe(self.state, event, payload) or self.dirty
            if self.dirty and time.monotonic() - self.flushed >= FLUSH_SEC:
                await self.flush()
        except Exception as exc:
            log.debug("Personal telemetry observe failed (%s)", type(exc).__name__)

    async def flush(self):
        import asyncio
        if not self.dirty:
            return
        self.dirty, self.flushed = False, time.monotonic()
        try:
            await asyncio.to_thread(self._save)
        except Exception as exc:
            log.debug("Personal telemetry write failed (%s)", type(exc).__name__)

    def _save(self):
        with engine.SessionLocal() as db:
            existing = db.scalar(select(PersonalAssignmentTelemetry).where(
                PersonalAssignmentTelemetry.work_item_id == self.state.work_item_id,
                PersonalAssignmentTelemetry.run_id == self.state.run_id))
            if existing is not None and not self.loaded:
                # A later core run of the same Assignment continues the recording.
                self.loaded = True
                self.state.steps += existing.steps
                for name, count in (existing.tool_counts or {}).items():
                    self.state.tool_counts[name] = self.state.tool_counts.get(name, 0) + count
                self.state.recent_steps = (list(existing.recent_steps or []) + self.state.recent_steps)[-RECENT_STEPS:]
                self.state.prompt_tokens += existing.prompt_tokens
                self.state.completion_tokens += existing.completion_tokens
                self.state.last_progress_at = self.state.last_progress_at or existing.last_progress_at
                self.state.started_at = existing.started_at or self.state.started_at
            self.loaded = True
            save(db, self.state)


def usage(response):
    """Token counts of one model response, as the hook adapter passes it."""
    data = response.get("usage") if isinstance(response, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    prompt = data.get("input_tokens", data.get("prompt_tokens"))
    completion = data.get("output_tokens", data.get("completion_tokens"))
    if prompt is None and completion is None:
        return None
    return {"prompt_tokens": prompt or 0, "completion_tokens": completion or 0}


def hooks(work_item_id, assignment_id):
    """Profile hooks that record one core run of a task's Worker (SUPERAGENT_DESIGN §11.1)."""
    from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage

    recorder = Recorder(work_item_id, assignment_id)

    async def tool(invocation):
        args = invocation.data.get("raw_input", invocation.data.get("tool_args"))
        await recorder.observe("tool_call", {"name": invocation.operation_name, "args": args})
        return Decision()

    async def model(invocation):
        counts = usage(invocation.data.get("response"))
        if counts:
            await recorder.observe("context_usage", counts)
        return Decision()

    async def finish(invocation):
        await recorder.flush()
        return Decision()

    return [
        HookSpec("personal_telemetry_tool", HookStage.BEFORE_TOOL, tool, priority=5, timeout_ms=5000, failure_mode=FailureMode.OPEN),
        HookSpec("personal_telemetry_model", HookStage.AFTER_MODEL, model, priority=5, timeout_ms=5000, failure_mode=FailureMode.OPEN),
        HookSpec("personal_telemetry_finish", HookStage.BEFORE_FINISH, finish, priority=5, timeout_ms=5000, failure_mode=FailureMode.OPEN),
    ]


def summaries(db, active_only=True):
    """Telemetry of the current run of every running task (or of every recorded run)."""
    from personal_agent.models import PersonalWorkItem

    query = select(PersonalAssignmentTelemetry, PersonalWorkItem.status).join(
        PersonalWorkItem, PersonalWorkItem.id == PersonalAssignmentTelemetry.work_item_id)
    if active_only:
        query = query.where(PersonalWorkItem.status == "running", PersonalWorkItem.run_id == PersonalAssignmentTelemetry.run_id)
    result = []
    for row, status in db.execute(query.order_by(PersonalAssignmentTelemetry.updated_at.desc()).limit(200)).all():
        digests = [digest for _, digest in row.recent_steps or []]
        result.append({
            "work_item_id": row.work_item_id, "run_id": row.run_id, "status": status,
            "steps": row.steps, "tool_counts": row.tool_counts or {},
            "recent_tools": [name for name, _ in row.recent_steps or []],
            "distinct_recent_steps": len(set(digests)),
            "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
            "last_progress_at": row.last_progress_at and row.last_progress_at.isoformat(),
            "started_at": row.started_at and row.started_at.isoformat(),
            "updated_at": row.updated_at and row.updated_at.isoformat(),
        })
    return result
