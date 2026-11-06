"""Chat sandbox snapshot jobs and pending deletions: no controller snapshot goes untracked.

The idle parker used to wait for a snapshot under a local deadline and forget it
when the deadline passed. The controller kept working, so a snapshot that became
Ready was lost, one that Failed was never deleted, and the next round started
another. Each in-flight or failed attempt now has a durable row, and every
snapshot nothing references any more is queued for deletion until the
controller confirms it.

Same schema as the EE revision ``sbxjob01``.

Revision ID: ce_0040
Revises: ce_0039
"""
from alembic import op
import sqlalchemy as sa

from core.db.utc_datetime import UTCDateTime

revision = "ce_0040"
down_revision = "ce_0039"
branch_labels = None
depends_on = None


def upgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "sandbox_snapshot_deletions" not in tables:
        op.create_table(
            "sandbox_snapshot_deletions",
            sa.Column("snapshot_id", sa.String(64), primary_key=True),
            sa.Column("chat_id", sa.String(64)),
            sa.Column("reason", sa.String(200)),
            sa.Column("queued_at", UTCDateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.String(500)),
        )
    if "chat_sandbox_snapshot_jobs" in tables:
        return
    op.create_table(
        "chat_sandbox_snapshot_jobs",
        sa.Column("chat_id", sa.String(64), primary_key=True),
        sa.Column("sandbox_id", sa.String(64), nullable=False),
        sa.Column("snapshot_id", sa.String(64)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("started_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_after", UTCDateTime(timezone=True)),
        sa.Column("last_error", sa.String(500)),
        sa.CheckConstraint(
            "state IN ('creating', 'failed')",
            name="chat_sandbox_snapshot_jobs_state_check",
        ),
    )
    op.create_index(
        "idx_chat_sandbox_snapshot_jobs_state", "chat_sandbox_snapshot_jobs", ["state"]
    )


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    for table in ("chat_sandbox_snapshot_jobs", "sandbox_snapshot_deletions"):
        if table in tables:
            op.drop_table(table)
