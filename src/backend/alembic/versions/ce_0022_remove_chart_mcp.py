"""Retire chart MCP registration, bindings and model role."""

from alembic import op
from core.db.retire_chart_mcp import retire_chart_mcp

revision = "ce_0022"
down_revision = "ce_0021"
branch_labels = None
depends_on = None


def upgrade():
    retire_chart_mcp(op.get_bind())


def downgrade():
    # The executable has been removed; do not restore unusable configuration.
    pass
