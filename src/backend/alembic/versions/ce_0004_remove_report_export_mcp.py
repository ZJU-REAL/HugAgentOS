"""Remove the retired report-export MCP from upgraded CE databases.

Revision ID: ce_0004
Revises: ce_0003
Create Date: 2026-08-02
"""

import sqlalchemy as sa
from alembic import op

revision = "ce_0004"
down_revision = "ce_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM mcp_market_installations WHERE slug IN "
            "(SELECT slug FROM mcp_market_versions "
            "WHERE source_server_id = 'report_export_mcp')"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM marketplace_listing_states WHERE kind = 'mcp' "
            "AND item_id IN (SELECT slug FROM mcp_market_versions "
            "WHERE source_server_id = 'report_export_mcp')"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM mcp_market_items WHERE slug IN "
            "(SELECT slug FROM mcp_market_versions "
            "WHERE source_server_id = 'report_export_mcp')"
        )
    )
    bind.execute(sa.text("DELETE FROM admin_mcp_servers WHERE server_id = 'report_export_mcp'"))


def downgrade() -> None:
    pass
