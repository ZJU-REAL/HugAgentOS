"""Reconcile the CE schema for the MCP marketplace and auth contract.

Revision ID: ce_0003
Revises: ce_0002
Create Date: 2026-08-02
"""

from alembic import op

revision = "ce_0003"
down_revision = "ce_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CE intentionally owns an independent migration chain.  Reusing the CE
    # metadata reconciler creates the new core marketplace tables/columns while
    # continuing to omit every EE-only table and foreign-key dependency.
    from core.db.edition_tables import ce_reconcile_schema

    ce_reconcile_schema(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("CE schema reconciliation does not support downgrade")
