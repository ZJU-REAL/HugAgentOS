"""Reconcile canonical model-step history for existing CE installations."""
from alembic import op

revision = "ce_0012"
down_revision = "ce_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.edition_tables import ce_reconcile_schema

    ce_reconcile_schema(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("Model-step reconciliation preserves conversation history")

