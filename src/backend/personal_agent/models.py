"""Persistent identities and work items; execution details stay in ChatRun."""
from sqlalchemy import Column, String, Text, Integer, Float, JSON, ForeignKey, UniqueConstraint, Index
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
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)

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
    """Durable routing admission, separate from task execution."""
    __tablename__ = "personal_message_inbox"
    message_id = Column(String(64), ForeignKey("chat_messages.message_id", ondelete="CASCADE"), primary_key=True)
    agent_id = Column(String(64), ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False)
    request_key = Column(String(100), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(24), nullable=False, default="pending")
    decision = Column(JSON, nullable=False, default=dict)
    question = Column(Text)
    owner = Column(String(100))
    lease_until = Column(Float)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint("agent_id", "request_key", name="uq_pa_inbox_request"), Index("ix_pa_inbox_status", "status", "created_at"))

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
