"""Use direct path delivery in stock desktop prompts."""

from alembic import op

revision = "ce_0016"
down_revision = "ce_0015"
branch_labels = None
depends_on = None


def upgrade():
    from core.db.desktop_delivery_repair import upgrade_desktop_delivery_prompts

    upgrade_desktop_delivery_prompts(op.get_bind())


def downgrade():
    raise NotImplementedError("Restore prompt versions from a pre-upgrade snapshot")
