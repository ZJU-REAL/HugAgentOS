"""Personal Agent devices: what the desktop says about itself.

A desktop reports its session contract (hugagent_cli.contract.contract_version) and app
version with each heartbeat. `personal_devices.link_info` keeps them, so the cloud can tell
the agent that an out-of-date desktop may need an update instead of only seeing failures.

Same schema as the EE revision ``padevice02``.

Revision ID: ce_0045
Revises: ce_0044
"""
from alembic import op
import sqlalchemy as sa

revision = "ce_0045"
down_revision = "ce_0044"
branch_labels = None
depends_on = None

TABLE, COLUMN = "personal_devices", "link_info"


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return
    if COLUMN not in {column["name"] for column in inspector.get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column(COLUMN, sa.JSON(), nullable=True))


def downgrade():
    op.drop_column(TABLE, COLUMN)
