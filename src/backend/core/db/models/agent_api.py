"""API admission metadata; execution outcome remains authoritative in ChatRun."""
from core.db.utc_datetime import UTCDateTime

from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Column, Index, Integer, String
from core.db.engine import Base


class AgentApiCallLog(Base):
    __tablename__ = "agent_api_call_logs"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    api_key_id = Column(String(64), nullable=False)
    agent_id = Column(String(64), nullable=False)
    # Snapshots keep historical records understandable after key revocation.
    key_name = Column(String(128), nullable=False)
    key_prefix = Column(String(32), nullable=False)
    chat_id = Column(String(100))
    run_id = Column(String(64), unique=True)
    trace_id = Column(String(64))
    stream = Column(Boolean, nullable=False, default=False)
    status = Column(String(24), nullable=False, default="running")
    http_status = Column(Integer)
    error_code = Column(String(64))
    created_at = Column(
        UTCDateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at = Column(UTCDateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','failed','cancelled','needs_attention')",
            name="agent_api_call_status_check",
        ),
        Index("idx_agent_api_calls_owner_agent_created", "user_id", "agent_id", "created_at"),
        Index("idx_agent_api_calls_key_created", "api_key_id", "created_at"),
    )
