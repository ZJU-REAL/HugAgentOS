"""Add durable tool media to existing CE installations."""
from alembic import op

revision = "ce_0013"
down_revision = "ce_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.edition_tables import ce_reconcile_schema

    ce_reconcile_schema(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("Tool media migration preserves conversation history")
