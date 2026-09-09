"""Reconcile desktop delivery, receipts and observation schema for existing CE installs."""
from alembic import op

revision = "ce_0009"
down_revision = "ce_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.edition_tables import ce_reconcile_schema
    ce_reconcile_schema(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("Desktop delivery reconciliation preserves user data")
