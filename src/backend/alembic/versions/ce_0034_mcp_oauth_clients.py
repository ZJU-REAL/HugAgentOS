"""Store administrator-registered OAuth Apps for MCP marketplace listings.

Revision ID: ce_0034
Revises: ce_0033
"""

import sqlalchemy as sa
from alembic import op

revision = "ce_0034"
down_revision = "ce_0033"
branch_labels = None
depends_on = None


def upgrade():
    # CE's initial create_all may already contain the current model tables.
    if "mcp_market_oauth_clients" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "mcp_market_oauth_clients",
        sa.Column(
            "slug",
            sa.String(128),
            sa.ForeignKey("mcp_market_items.slug", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("client_id", sa.String(512), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("mcp_market_oauth_clients")
