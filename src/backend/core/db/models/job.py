"""SQLAlchemy ORM models — 作业编排运行时（Job Runtime）。

一个 **job** 是「对 N 个同构工作项批量作业」的一次执行：主对话智能体写一段作业脚本，
脚本跑在沙箱里（普通 Python 进程，拥有文件/网络/并发），需要模型判断时通过带 job token
的回调请求后端派子智能体——模型凭据因此永远不进沙箱。

三张表的分工：

- ``jobs``       一次作业的元信息、预算、用量、脚本快照（供审计与 resume 原样续跑）
- ``job_items``  工作项台账，**唯一真源**。按 (job_id, item_key) 复合主键幂等，
                 断点续跑靠它：重跑脚本时 ``pending()`` 自动跳过已完成项。
- ``job_calls``  每次子作业调用的审计与成本归因

台账放 DB 而不是沙箱文件：沙箱池化复用会让新 job 看见旧 job 残留的账本文件
（autonomous_loop 踩过这个坑，靠盖 loop_id 章解决），以 job_id 为主键从结构上避免。
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, BigInteger, Text, TIMESTAMP,
    ForeignKey, CheckConstraint, Index, JSON
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from core.db.engine import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    """带时区的「现在」。

    ⚠️ 这里**不能**用 `datetime.utcnow`：它给的是 naive 值，写进 `TIMESTAMP(timezone=True)`
    时由 PostgreSQL 按**会话时区**解释。生产/测试机容器都是 `TZ=Asia/Shanghai`，于是
    naive 的 UTC 时刻被当成 +08 存下来，落库瞬间就比真实时刻早 8 小时——状态条上一条刚
    提交的作业因此显示「已运行 8 小时」（实测于 HugAgentOS 测试机）。带 tzinfo 的值不受
    会话时区影响，写进去是哪个时刻读出来就是哪个时刻。
    """
    return datetime.now(timezone.utc)


# 终态：驱动不再推进，token 失效
JOB_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
# 活跃态：进程重启后需要对账（归位 interrupted 或按需续跑）
JOB_LIVE_STATUSES = ("pending", "running")


class Job(Base):
    """一次批量作业。"""
    __tablename__ = "jobs"

    job_id             = Column(String(64), primary_key=True)
    user_id            = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False)
    chat_id            = Column(String(64))
    name               = Column(String(255), default="")

    status             = Column(String(20), nullable=False, default="pending")
    # 脚本正文随 job 快照，便于审计与「改一行再 resume」；script_path 是沙箱内路径
    script_path        = Column(Text, default="")
    script_text        = Column(Text, default="")
    sandbox_session_id = Column(String(128))

    # budget: {max_calls, max_tokens, max_seconds, concurrency}
    budget             = Column(JSONType, default=dict)
    # usage:  {calls, tokens, seconds}
    usage              = Column(JSONType, default=dict)
    # extra_data.start_params：发起时的运行时参数（模型/工具面/思考档位），resume 原样续跑不降级
    extra_data         = Column("metadata", JSONType, default=dict)

    error_message      = Column(Text)
    created_at         = Column(TIMESTAMP(timezone=True), default=_utcnow)
    started_at         = Column(TIMESTAMP(timezone=True))
    completed_at       = Column(TIMESTAMP(timezone=True))
    updated_at         = Column(TIMESTAMP(timezone=True), default=_utcnow, onupdate=_utcnow)

    items = relationship("JobItem", back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','paused','completed','failed','cancelled','interrupted')",
            name="jobs_status_check",
        ),
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_chat_status", "chat_id", "status"),
        Index("idx_jobs_status", "status"),
    )


class JobItem(Base):
    """工作项台账 —— 完成判据的唯一真源。"""
    __tablename__ = "job_items"

    job_id     = Column(String(64), ForeignKey("jobs.job_id", ondelete="CASCADE"),
                        primary_key=True, nullable=False)
    item_key   = Column(String(128), primary_key=True, nullable=False)

    status     = Column(String(20), nullable=False, default="pending")
    payload    = Column(JSONType, default=dict)   # seed 时给的输入
    result     = Column(JSONType)                 # 子作业产出（schema 校验后）
    review     = Column(JSONType)                 # 验收裁决（可多轮，键为规格名）
    attempts   = Column(Integer, default=0)
    error      = Column(Text)
    updated_at = Column(TIMESTAMP(timezone=True), default=_utcnow, onupdate=_utcnow)

    job = relationship("Job", back_populates="items")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','done','not_found','failed','needs_review')",
            name="job_items_status_check",
        ),
        # pending() 的查询路径
        Index("idx_job_items_job_status", "job_id", "status"),
    )


class JobCall(Base):
    """每次 ``agent()`` 回调的审计与成本归因。"""
    __tablename__ = "job_calls"

    call_id     = Column(String(64), primary_key=True)
    job_id      = Column(String(64), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False)
    item_key    = Column(String(128))
    seq         = Column(Integer, default=0)

    prompt_hash = Column(String(64))
    model       = Column(String(128))
    tokens      = Column(JSONType, default=dict)
    duration_ms = Column(BigInteger, default=0)
    status      = Column(String(20), default="running")
    error       = Column(Text)
    created_at  = Column(TIMESTAMP(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("idx_job_calls_job_id", "job_id"),
    )
