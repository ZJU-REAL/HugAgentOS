"""Add the derived history preview to existing CE installations."""
from alembic import op

revision = "ce_0019"
down_revision = "ce_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.edition_tables import ce_reconcile_schema

    ce_reconcile_schema(op.get_bind())


def downgrade() -> None:
    op.drop_column("chat_messages", "tool_calls_display")
