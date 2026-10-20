"""Durable ordinary-chat command executions and their terminal delivery state."""
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, Integer, Boolean, Float, JSON, ForeignKey, Index, CheckConstraint, UniqueConstraint
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime

class CommandTask(Base):
    __tablename__ = "command_tasks"
    task_id = Column(String(64), primary_key=True)
    chat_id = Column(String(64), ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(64), nullable=False)
    sandbox_session_id = Column(String(128), nullable=False)
    source_run_id = Column(String(64))
    cancel_requested = Column(Boolean, nullable=False, default=False)
    process_id = Column(String(128))
    execution_ref = Column(JSON)
    result_metadata = Column(JSON, nullable=False, default=dict)
    command = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="starting")
    background = Column(Boolean, nullable=False, default=False)
    deadline_at = Column(Float)
    owner = Column(String(160))
    lease_until = Column(Float, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=0)
    stdout = Column(Text, nullable=False, default="")
    stderr = Column(Text, nullable=False, default="")
    stdout_base = Column(Integer, nullable=False, default=0)
    stderr_base = Column(Integer, nullable=False, default=0)
    readers = Column(JSON, nullable=False, default=dict)
    exit_code = Column(Integer)
    error = Column(Text)
    delivery_mode = Column(String(20))
    notice_status = Column(String(20), nullable=False, default="none")
    notice_run_id = Column(String(64))
    created_at = Column(UTCDateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    finished_at = Column(UTCDateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("sandbox_session_id", "user_id", "process_id", name="uq_command_owned_process"),
        Index("idx_command_chat_status", "chat_id", "status"),
        Index("idx_command_notice", "notice_status", "created_at"),
        CheckConstraint("status IN ('starting','running','completed','failed','cancelled','unknown')",
                        name="command_status_check"),
        CheckConstraint("notice_status IN ('none','pending','reserved','delivered','consumed')",
                        name="command_notice_check"),
    )
