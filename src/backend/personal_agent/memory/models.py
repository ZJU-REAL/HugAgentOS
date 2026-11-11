"""The write log: one row per change to a memory file, kept for 30 days.

The owner sees who changed what and when, and can revert one change; Dreaming reads the
online changes of each source to verify them against the conversation they came from.
"""
from sqlalchemy import Column, ForeignKey, Index, Integer, String, Text

from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now


class PersonalMemoryVersion(Base):
    __tablename__ = "personal_memory_versions"
    id = Column(String(64), primary_key=True)
    agent_id = Column(String(64), ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False)
    path = Column(String(255), nullable=False)          # workspace-relative, e.g. memory/person-zhang-san.md
    writer = Column(String(16), nullable=False)         # layout.WRITERS
    source_id = Column(String(160))                     # sources.SOURCE_ID; None for user and system writes
    run_id = Column(String(64))                         # the Dreaming run of an analyzer or organizer write
    before_hash = Column(String(64))                    # None: the file did not exist
    after_hash = Column(String(64))                     # None: the file was deleted
    before_text = Column(Text)                          # kept so one change can be reverted exactly
    after_text = Column(Text)
    bytes_changed = Column(Integer, nullable=False, default=0)
    reverted_by = Column(String(64))                    # the version that undid this one
    created_at = Column(UTCDateTime(timezone=True), default=utc_now, nullable=False)
    __table_args__ = (
        Index("ix_pa_memory_versions_agent", "agent_id", "created_at"),
        Index("ix_pa_memory_versions_path", "agent_id", "path", "created_at"),
        Index("ix_pa_memory_versions_source", "agent_id", "source_id"),
    )


class PersonalMemoryUsage(Base):
    """When the agent last used a page, for archiving and deleting what nobody reads (memory.lifecycle).

    One row per page: a front read or a search hit sets last_used_at; first_seen_at starts the
    clock of a page nobody has used yet; archived_at is when the platform archived it."""
    __tablename__ = "personal_memory_usage"
    agent_id = Column(String(64), ForeignKey("personal_agents.id", ondelete="CASCADE"), primary_key=True)
    path = Column(String(255), primary_key=True)        # memory/<slug>.md
    first_seen_at = Column(UTCDateTime(timezone=True), default=utc_now, nullable=False)
    last_used_at = Column(UTCDateTime(timezone=True))
    uses = Column(Integer, nullable=False, default=0)
    archived_at = Column(UTCDateTime(timezone=True))
