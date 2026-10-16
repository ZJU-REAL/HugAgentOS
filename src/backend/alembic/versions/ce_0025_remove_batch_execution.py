"""Retire Batch execution and delete its persisted data.

Revision ID: ce_0025
Revises: ce_0024

Back up the database and stop old Batch workers before upgrading.
Data rollback requires restoring that backup; ordinary features are retained.
"""
from alembic import op

from core.db.batch_retirement import retire_batch_execution

revision = "ce_0025"
down_revision = "ce_0024"
branch_labels = None
depends_on = None


def upgrade():
    retire_batch_execution(op.get_bind())


def downgrade():
    raise RuntimeError(
        "Batch execution data was deleted. Restore the pre-upgrade database "
        "backup together with the previous application version to roll back."
    )
