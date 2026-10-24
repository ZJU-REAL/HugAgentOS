"""ORM models for message feedback, sandbox snapshots and tool media blobs."""
from __future__ import annotations
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from datetime import datetime, timezone
from core.db.engine import Base
from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, LargeBinary, Index, Integer, String, Text
from core.db.models.column_types import BigIntPK, JSONType

class MessageFeedback(Base):
    """Message feedback table - stores like/dislike ratings and optional comments."""

    __tablename__ = "message_feedback"

    feedback_id = Column(BigIntPK, primary_key=True, autoincrement=True)
    message_id = Column(
        String(64),
        ForeignKey("chat_messages.message_id", ondelete="CASCADE"),
        nullable=False,
    )
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    rating = Column(String(10), nullable=False)  # 'like' or 'dislike'
    comment = Column(Text, nullable=True)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(
        UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        CheckConstraint(
            "rating IN ('like', 'dislike')", name="message_feedback_rating_check"
        ),
        Index("idx_message_feedback_message_id", "message_id"),
        Index("idx_message_feedback_chat_id", "chat_id"),
        Index("idx_message_feedback_user_id", "user_id"),
    )


class ChatSandboxSnapshot(Base):
    """Per-chat opensandbox snapshot pointer.

    A chat keeps at most 1 snapshot at any time (the latest one). The background
    idle worker upserts on snapshot+kill; the next time that chat reconnects,
    _get_or_create_session prefers this snapshot when starting a new sandbox,
    restoring the filesystem state. The GC worker periodically scans rows with
    expires_at < now and deletes remote + DB together. Design in
    internal design docs.
    """

    __tablename__ = "chat_sandbox_snapshots"

    chat_id = Column(String(64), primary_key=True)
    snapshot_id = Column(String(64), nullable=False, unique=True)
    sandbox_id = Column(
        String(64), nullable=False
    )  # source sandbox id parked at the time, for debugging / reconciliation
    created_at = Column(
        UTCDateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(
        UTCDateTime(timezone=True), nullable=False
    )  # created_at + SNAPSHOT_RETENTION_DAYS
    size_bytes = Column(BigInteger)  # for metrics, nullable
    extra = Column(
        "metadata", JSONType, default=dict
    )  # reserved: image uri / pool kind / notes

    __table_args__ = (
        Index("idx_chat_sandbox_snapshots_expires", "expires_at"),
        Index("idx_chat_sandbox_snapshots_created", "created_at"),
    )


class ToolMediaBlob(Base):
    """工具返回的媒体字节，按内容寻址，供历史回放取回原件。

    ``model_steps`` 是「模型自己的历史」的唯一真源，回放它应当得到模型当时真正看过
    的那串消息。媒体曾经是这条真源上唯一的缺口：落库时图被换成一行
    ``[image omitted from replay history: …]``，于是图只能活一轮——下一轮重建历史时，
    模型看到的是一句「这里原本有张图」，而它不会说自己看不见，会拿手上剩下的信息接
    着编。

    字节单独放一张表、只在 ``model_steps`` 里留 ``sha256`` 指针，是为了不把历史列
    撑大：拼上下文要把检查点之后每一条消息的 ``model_steps`` 整列拉出来，一页 200dpi
    的图编码后一两兆，内联进去就意味着**每一轮**都要整份读一遍，哪怕预算马上要把那
    一步挤掉。主键用内容哈希，所以同一张图读多少次都只存一份。

    与历史行同库，所以备份、迁移、恢复都把两者一起带走，不存在第二个会和它走散的
    系统。字节先写、引用后写（引用是随那条消息落库的），所以中途崩溃最坏留下一份没人
    引用的字节，不会出现「引用还在、字节没了」。
    """

    __tablename__ = "tool_media_blobs"

    sha256 = Column(String(64), primary_key=True)
    media_type = Column(String(128), nullable=False)
    data = Column(LargeBinary, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    created_at = Column(
        UTCDateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (Index("idx_tool_media_blobs_created", "created_at"),)
