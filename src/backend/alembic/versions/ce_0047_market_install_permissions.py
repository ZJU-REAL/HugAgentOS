"""Preserve existing personal marketplace access when splitting install permissions."""
from alembic import op

revision = "ce_0047"
down_revision = "ce_0046"
branch_labels = None
depends_on = None


def upgrade():
    from core.db.data_repair import (
        MARKET_PERMISSION_SPLIT,
        PLUGIN_AGENT_MARKET_SPLIT,
        copy_permission_bits,
    )

    copy_permission_bits(op.get_bind(), MARKET_PERMISSION_SPLIT + PLUGIN_AGENT_MARKET_SPLIT)


def downgrade():
    # Previous versions ignore the split settings.
    pass
