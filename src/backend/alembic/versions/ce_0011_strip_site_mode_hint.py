"""Strip the site-mode working rules out of stored user messages (CE chain)."""

from alembic import op

revision = "ce_0011"
down_revision = "ce_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.data_repair import strip_site_mode_hint_from_user_messages

    strip_site_mode_hint_from_user_messages(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("Site-hint cleanup only removes model-facing text")
