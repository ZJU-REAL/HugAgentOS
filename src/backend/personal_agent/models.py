"""Persistent identities and work items; execution details stay in ChatRun."""
from sqlalchemy import Column, String, Text, Integer, Float, JSON, ForeignKey, UniqueConstraint, Index, event
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now

class PersonalAgent(Base):
    __tablename__ = "personal_agents"
    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), unique=True, nullable=False)
    chat_id = Column(String(64), ForeignKey("chat_sessions.chat_id", ondelete="RESTRICT"), unique=True, nullable=False)
    sandbox_session_id = Column(String(64), unique=True, nullable=False)
    sandbox_id = Column(String(128))
    execd_secret_enc = Column(Text)
    generation = Column(Integer, nullable=False, default=1)
    status = Column(String(24), nullable=False, default="ready")
    computer_status = Column(String(24), nullable=False, default="not_provisioned")
    config = Column(JSON, nullable=False, default=dict)
    # The earliest time anything of this agent is due (a schedule, the proactive check), as an epoch;
    # NULL when nothing is. Derived from config by scheduling.schedules.store, so that the scheduler
    # finds the due agents by an index instead of reading every config.
    next_due_at = Column(Float, index=True)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)

@event.listens_for(PersonalAgent.config, "set")
def _keep_due_time(agent, config, old, initiator):
    """Every assignment of config also works out next_due_at, whoever writes it and by whatever path.
    Many modules write this document (memory, channels, persona); one that writes back a config it
    read earlier would otherwise leave the column and the times in the config apart until a restart.
    A statement that updates the config column directly does not pass here: use schedules.store."""
    from personal_agent.scheduling.schedules import due_at
    agent.next_due_at = due_at(config)

# A task's short name: the title the agent gave it, or the start of its goal.
TITLE_LENGTH = 80

class PersonalWorkItem(Base):
    __tablename__ = "personal_work_items"
    id = Column(String(64), primary_key=True)
    agent_id = Column(String(64), ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(String(64), ForeignKey("chat_sessions.chat_id", ondelete="RESTRICT"), unique=True)
    request_key = Column(String(100), nullable=False)
    goal = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="queued")
    run_id = Column(String(64))
    context_seq = Column(Integer, nullable=False, default=0)
    waiting_reason = Column(String(300))
    result = Column(Text)
    checkpoint = Column(JSON, nullable=False, default=dict)
    owner = Column(String(100))
    lease_until = Column(Float)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    __table_args__ = (UniqueConstraint("agent_id", "request_key", name="uq_pa_request"), Index("ix_pa_work_status", "status", "created_at"))

    def requeue(self):
        """Back to the queue: no owner, no lease, no waiting reason."""
        self.status, self.waiting_reason, self.owner, self.lease_until = "queued", None, None, None

    def pause(self, reason):
        self.status, self.waiting_reason = "waiting", reason

    @property
    def label_text(self):
        """The short name the agent gave the task, or the start of its goal."""
        return ((self.checkpoint or {}).get("title") or self.goal)[:TITLE_LENGTH]

    @property
    def label(self):
        return "任务「" + self.label_text[:60] + "」"

    def unconfirmed(self):
        """True when an external effect of this task may or may not have happened."""
        return any(item.get("status") == "uncertain" or item.get("outcome_unknown")
                   for item in (self.checkpoint or {}).get("action_receipts", {}).values())

    def pop_state(self, key):
        """Remove one checkpoint key and return its value."""
        if key not in (self.checkpoint or {}): return None
        state = dict(self.checkpoint)
        value = state.pop(key)
        self.checkpoint = state
        return value

class PersonalAssignment(Base):
    """One binding of a task to its Worker session: pending -> running -> ended.

    The Worker runs as ordinary core ChatRuns on ``chat_id``; ``run_id`` is the latest one.
    While an assignment runs, the task mirrors its id, owner and lease in
    ``PersonalWorkItem.run_id / owner / lease_until``, which the task tools fence on."""
    __tablename__ = "personal_assignments"
    id = Column(String(64), primary_key=True)
    work_item_id = Column(String(64), ForeignKey("personal_work_items.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(String(64), ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    # task: one of the agent's concurrency slots; follow_up: the extra slot for a user's follow-up.
    slot = Column(String(24), nullable=False, default="task")
    run_id = Column(String(64))
    owner = Column(String(100))
    lease_until = Column(Float)
    retries = Column(Integer, nullable=False, default=0)
    retry_at = Column(Float)
    # Set while no run is live but background commands still are (BACKGROUND_LIMIT).
    idle_since = Column(Float)
    ended_reason = Column(String(40))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    ended_at = Column(UTCDateTime(timezone=True))
    __table_args__ = (Index("ix_pa_assignment_task", "work_item_id", "status"),
                      Index("ix_pa_assignment_status", "status", "lease_until"))

class PersonalDevice(Base):
    __tablename__ = "personal_devices"
    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    platform = Column(String(24), nullable=False)
    secret_hash = Column(String(64), nullable=False)
    capabilities = Column(JSON, nullable=False, default=list)
    epoch = Column(Integer, nullable=False, default=1)
    revoked = Column(Integer, nullable=False, default=0)
    last_seen = Column(Float)
    control_enabled = Column(Integer, nullable=False, default=0)
    sessions_enabled = Column(Integer, nullable=False, default=0)
    session_epoch = Column(Integer, nullable=False, default=1)

class PersonalDeviceCommand(Base):
    __tablename__ = "personal_device_commands"
    id = Column(String(64), primary_key=True)
    device_id = Column(String(64), ForeignKey("personal_devices.id", ondelete="CASCADE"), nullable=False, index=True)
    epoch = Column(Integer, nullable=False)
    work_item_id = Column(String(64), ForeignKey("personal_work_items.id", ondelete="CASCADE"), nullable=False)
    run_id = Column(String(64))
    action = Column(JSON, nullable=False)
    status = Column(String(24), nullable=False, default="queued")
    result = Column(JSON)
    deadline = Column(Float, nullable=False)

class PersonalMessageTask(Base):
    """One message can give different instructions to several tasks."""
    __tablename__ = "personal_message_tasks"
    message_id = Column(String(64), ForeignKey("chat_messages.message_id", ondelete="CASCADE"), primary_key=True)
    work_item_id = Column(String(64), ForeignKey("personal_work_items.id", ondelete="CASCADE"), primary_key=True)
    relation = Column(String(24), primary_key=True)
    instruction = Column(Text, nullable=False, default="")
    __table_args__ = (Index("ix_pa_message_task", "work_item_id", "message_id"),)

class PersonalMessageInbox(Base):
    """A Trigger of the agent's own turn: something that happened in one conversation (the lane).

    It holds no lease; the Turn that claimed it does (see PersonalFrontTurn)."""
    __tablename__ = "personal_message_inbox"
    message_id = Column(String(64), ForeignKey("chat_messages.message_id", ondelete="CASCADE"), primary_key=True)
    agent_id = Column(String(64), ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False)
    request_key = Column(String(100), nullable=False)
    # message, task_result, session_run, heartbeat or bootstrap (see conversation/router/kinds.py).
    kind = Column(String(24), nullable=False, default="message", server_default="message")
    chat_id = Column(String(64))
    turn_id = Column(String(64))
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(24), nullable=False, default="pending")
    decision = Column(JSON, nullable=False, default=dict)
    question = Column(Text)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint("agent_id", "request_key", name="uq_pa_inbox_request"), Index("ix_pa_inbox_status", "status", "created_at"),
                      Index("ix_pa_inbox_turn", "turn_id"))

class PersonalFrontTurn(Base):
    """One run of the agent's own turn over a batch of Triggers of one conversation. The lease is here."""
    __tablename__ = "personal_front_turns"
    id = Column(String(64), primary_key=True)
    agent_id = Column(String(64), ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default="running")
    owner = Column(String(100))
    lease_until = Column(Float)
    attempts = Column(Integer, nullable=False, default=1)
    reply_ids = Column(JSON, nullable=False, default=list)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    finished_at = Column(UTCDateTime(timezone=True))
    __table_args__ = (Index("ix_pa_front_turn_agent", "agent_id", "status"),)

class PersonalSessionOperation(Base):
    """Durable session operation receipt, independent of GUI control."""
    __tablename__ = "personal_session_operations"
    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False)
    device_id = Column(String(64), ForeignKey("personal_devices.id", ondelete="CASCADE"))
    target = Column(String(100), nullable=False)
    request_key = Column(String(100), nullable=False)
    operation = Column(String(40), nullable=False)
    args = Column(JSON, nullable=False)
    work_item_id = Column(String(64), ForeignKey("personal_work_items.id", ondelete="CASCADE"))
    run_id = Column(String(64))
    task_owner = Column(String(100))
    epoch = Column(Integer, nullable=False, default=1)
    status = Column(String(24), nullable=False, default="queued")
    result = Column(JSON)
    deadline = Column(Float, nullable=False)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint("user_id", "request_key", name="uq_pa_session_request"),
                     Index("ix_pa_session_queue", "device_id", "status", "deadline"))


# Schema registration imports every installed package's ``models`` module, and nothing
# else, before the database is created or reconciled. These two tables live next to the
# code that writes them; without these imports the desktop database (no Alembic) never
# got them and every health beat and assignment record failed with "no such table".
from personal_agent.health import PersonalRuntimeHealth  # noqa: E402,F401
from personal_agent.telemetry import PersonalAssignmentTelemetry  # noqa: E402,F401
from personal_agent.memory.models import PersonalMemoryUsage, PersonalMemoryVersion  # noqa: E402,F401
