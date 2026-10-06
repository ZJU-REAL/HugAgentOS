"""Register plugin resource tables for existing community installations."""
from alembic import op

revision = "ce_0020"
down_revision = "ce_0019"
branch_labels = None
depends_on = None


def upgrade():
    from core.db.edition_tables import ce_reconcile_schema
    ce_reconcile_schema(op.get_bind())


def downgrade():
    for table in ("plugin_resource_asset_tickets", "plugin_resource_tickets", "plugin_resource_checkpoints", "plugin_resources", "plugin_resource_packages"):
        op.drop_table(table)
