"""Add exclusive Workbench resource ownership without losing conversation rows.

Revision ID: ce_0026
Revises: ce_0025
"""
from alembic import op
from core.db.resource_owner_upgrade import upgrade_resource_owners, downgrade_resource_owners

revision = "ce_0026"
down_revision = "ce_0025"
branch_labels = None
depends_on = None


def upgrade():
    upgrade_resource_owners(op.get_bind())


def downgrade():
    downgrade_resource_owners(op.get_bind())
