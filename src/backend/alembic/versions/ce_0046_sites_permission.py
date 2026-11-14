"""Preserve existing personal site access when splitting the lab permission."""
from alembic import op

revision = "ce_0046"
down_revision = "ce_0045"
branch_labels = None
depends_on = None


def upgrade():
    from core.db.data_repair import copy_lab_permission_to_sites
    copy_lab_permission_to_sites(op.get_bind())


def downgrade():
    # Previous versions ignore the separate permission; retain the explicit setting.
    pass
