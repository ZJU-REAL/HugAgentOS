"""CE: add job runtime tables (jobs / job_items / job_calls)

Revision ID: ce_0006
Revises: ce_0005
Create Date: 2026-08-17

作业编排运行时的持久化底座。CE 的 agent_factory 一直注册着 `run_job` 工具、
工作流模式的提示词也在，但这三张表从来没进过 CE 迁移链——作业一提交就会在
建台账那一步炸掉。这条迁移把 CE 补齐到与主仓一致。

台账（job_items）放 DB 而不是沙箱文件：沙箱池化复用会让新 job 读到旧 job 残留的
账本，以 (job_id, item_key) 作复合主键从结构上避免。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ce_0006"
down_revision = "ce_0005"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.String(64)),
        sa.Column("name", sa.String(255), server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("script_path", sa.Text(), server_default=""),
        sa.Column("script_text", sa.Text(), server_default=""),
        sa.Column("sandbox_session_id", sa.String(128)),
        sa.Column("budget", JSONB),
        sa.Column("usage", JSONB),
        sa.Column("metadata", JSONB),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users_shadow.user_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('pending','running','paused','completed','failed','cancelled','interrupted')",
            name="jobs_status_check",
        ),
    )
    op.create_index("idx_jobs_user_id", "jobs", ["user_id"])
    op.create_index("idx_jobs_chat_status", "jobs", ["chat_id", "status"])
    op.create_index("idx_jobs_status", "jobs", ["status"])

    op.create_table(
        "job_items",
        sa.Column("job_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("item_key", sa.String(128), primary_key=True, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("payload", JSONB),
        sa.Column("result", JSONB),
        sa.Column("review", JSONB),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('pending','running','done','not_found','failed','needs_review')",
            name="job_items_status_check",
        ),
    )
    op.create_index("idx_job_items_job_status", "job_items", ["job_id", "status"])

    op.create_table(
        "job_calls",
        sa.Column("call_id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("item_key", sa.String(128)),
        sa.Column("seq", sa.Integer(), server_default="0"),
        sa.Column("prompt_hash", sa.String(64)),
        sa.Column("model", sa.String(128)),
        sa.Column("tokens", JSONB),
        sa.Column("duration_ms", sa.BigInteger(), server_default="0"),
        sa.Column("status", sa.String(20), server_default="running"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
    )
    op.create_index("idx_job_calls_job_id", "job_calls", ["job_id"])


def downgrade() -> None:
    op.drop_index("idx_job_calls_job_id", table_name="job_calls")
    op.drop_table("job_calls")
    op.drop_index("idx_job_items_job_status", table_name="job_items")
    op.drop_table("job_items")
    op.drop_index("idx_jobs_status", table_name="jobs")
    op.drop_index("idx_jobs_chat_status", table_name="jobs")
    op.drop_index("idx_jobs_user_id", table_name="jobs")
    op.drop_table("jobs")
