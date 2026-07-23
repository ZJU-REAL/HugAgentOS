"""Reconcile databases created by earlier dynamic CE baselines.

Revision ID: ce_0002
Revises: ce_0001
Create Date: 2026-07-23
"""

from alembic import op

revision = "ce_0002"
down_revision = "ce_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.edition_tables import ce_reconcile_schema

    ce_reconcile_schema(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("CE schema reconciliation does not support downgrade")
