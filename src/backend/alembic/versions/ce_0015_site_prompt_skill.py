"""Move legacy desktop site prompt rules to the site-builder skill."""
from alembic import op

revision = "ce_0015"
down_revision = "ce_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.data_repair import remove_desktop_site_prompt_parts

    remove_desktop_site_prompt_parts(op.get_bind())


def downgrade() -> None:
    # Retired prompt parts may contain customized text and cannot be reconstructed.
    raise NotImplementedError("Restore prompt versions from a pre-upgrade snapshot")
