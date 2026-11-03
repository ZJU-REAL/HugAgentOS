"""Personal Agent Assignments: a task's Worker runs on the core chat runtime (SUPERAGENT_DESIGN §5, stage 2).

Running tasks need no data change: the executor recovers a running task that has no
live assignment as an interrupted run (§6.5) once its old lease expires.

Same schema as the EE revision ``paassign01``.

Revision ID: ce_0039
Revises: ce_0038
"""
from alembic import op
import sqlalchemy as sa

from core.db.utc_datetime import UTCDateTime

revision = "ce_0039"
down_revision = "ce_0038"
branch_labels = None
depends_on = None


def upgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "personal_assignments" in tables:
        return
    op.create_table(
        "personal_assignments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("work_item_id", sa.String(64), sa.ForeignKey("personal_work_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.String(64), sa.ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("slot", sa.String(24), nullable=False, server_default="task"),
        sa.Column("run_id", sa.String(64)),
        sa.Column("owner", sa.String(100)),
        sa.Column("lease_until", sa.Float()),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_at", sa.Float()),
        sa.Column("idle_since", sa.Float()),
        sa.Column("ended_reason", sa.String(40)),
        sa.Column("created_at", UTCDateTime(timezone=True)),
        sa.Column("ended_at", UTCDateTime(timezone=True)),
    )
    op.create_index("ix_pa_assignment_task", "personal_assignments", ["work_item_id", "status"])
    op.create_index("ix_pa_assignment_status", "personal_assignments", ["status", "lease_until"])


def downgrade():
    if "personal_assignments" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("personal_assignments")
