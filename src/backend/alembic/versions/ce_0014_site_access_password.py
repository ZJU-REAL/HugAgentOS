"""Add site access password columns to existing CE installations."""
from alembic import op

revision = "ce_0014"
down_revision = "ce_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.edition import ce_reconcile_schema

    ce_reconcile_schema(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("Dropping the column would discard site access passwords")
