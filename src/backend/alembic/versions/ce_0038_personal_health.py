"""Personal Agent HealthBoard and per-run telemetry (SUPERAGENT_DESIGN §11.1, stage 1).

Same schema as the EE revision ``pahealth01``.

Revision ID: ce_0038
Revises: ce_0037
"""
from alembic import op
import sqlalchemy as sa

from core.db.utc_datetime import UTCDateTime

revision = "ce_0038"
down_revision = "ce_0037"
branch_labels = None
depends_on = None


def upgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "personal_runtime_health" not in tables:
        op.create_table(
            "personal_runtime_health",
            sa.Column("name", sa.String(100), primary_key=True),
            sa.Column("owner", sa.String(100), primary_key=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
            sa.Column("detail", sa.String(500)),
            sa.Column("interval_sec", sa.Float(), nullable=False, server_default="0"),
            sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failing_since", UTCDateTime(timezone=True)),
            sa.Column("beat_at", UTCDateTime(timezone=True)),
            sa.Column("updated_at", UTCDateTime(timezone=True)),
        )
    if "personal_assignment_telemetry" not in tables:
        op.create_table(
            "personal_assignment_telemetry",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("work_item_id", sa.String(64), nullable=False),
            sa.Column("run_id", sa.String(64), nullable=False),
            sa.Column("steps", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tool_counts", sa.JSON(), nullable=False),
            sa.Column("recent_steps", sa.JSON(), nullable=False),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_progress_at", UTCDateTime(timezone=True)),
            sa.Column("started_at", UTCDateTime(timezone=True)),
            sa.Column("updated_at", UTCDateTime(timezone=True)),
            sa.UniqueConstraint("work_item_id", "run_id", name="uq_pa_telemetry_run"),
        )


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    for table in ("personal_assignment_telemetry", "personal_runtime_health"):
        if table in tables:
            op.drop_table(table)
