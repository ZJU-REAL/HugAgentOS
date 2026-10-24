"""Normalize stored automation delivery destinations."""
from alembic import op

revision = "ce_0033"
down_revision = "ce_0032"
branch_labels = None
depends_on = None


def upgrade():
    from features.automation.schema import upgrade_delivery_targets
    upgrade_delivery_targets(op.get_bind())


def downgrade():
    # The earlier schema already reads delivery_targets; no lossy reverse conversion.
    pass
