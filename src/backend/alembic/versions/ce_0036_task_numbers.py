"""Task numbers: RW-YYYYMMDD-NNNN on every run, RW-…-NN on every sub-agent call.

Same schema and backfill as the EE revision ``tasknumber01`` (``core.db.task_number_migration``).

Revision ID: ce_0036
Revises: ce_0035
"""

import sqlalchemy as sa
from alembic import op

from core.db.task_number_migration import downgrade_task_numbers, upgrade_task_numbers

revision = "ce_0036"
down_revision = "ce_0035"
branch_labels = None
depends_on = None


def upgrade():
    # CE's initial create_all may already contain the current model tables.
    if "task_number_counters" in sa.inspect(op.get_bind()).get_table_names():
        return
    upgrade_task_numbers(op)


def downgrade():
    downgrade_task_numbers(op)
