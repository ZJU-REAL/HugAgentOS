"""Fence scheduled run completion with the current execution token."""

import sqlalchemy as sa
from alembic import op

revision = "ce_0031"
down_revision = "ce_0030"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scheduled_task_runs")}
    if "lease_token" not in columns:
        op.add_column("scheduled_task_runs", sa.Column("lease_token", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("scheduled_task_runs", "lease_token")
