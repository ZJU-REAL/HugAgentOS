"""Personal Agent devices: when a session operation was handed to its device.

A read-only operation whose answer never came back (the reply to the claim was lost on
the way, or the device restarted before running it) can be handed out again: reading
twice changes nothing. `claimed_at` says when it was handed out, so the cloud can tell a
lost claim from one that is still running.

Same schema as the EE revision ``padevice01``.

Revision ID: ce_0043
Revises: ce_0042
"""
from alembic import op
import sqlalchemy as sa

revision = "ce_0043"
down_revision = "ce_0042"
branch_labels = None
depends_on = None

TABLE, COLUMN = "personal_session_operations", "claimed_at"


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names(): return
    if COLUMN not in {column["name"] for column in inspector.get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column(COLUMN, sa.Float(), nullable=True))


def downgrade():
    op.drop_column(TABLE, COLUMN)
