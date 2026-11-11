"""Add Personal Agent memory history and usage tables to existing CE databases."""

from alembic import op
import sqlalchemy as sa

revision = "ce_0042"
down_revision = "ce_0041"
branch_labels = None
depends_on = None

VERSIONS_TABLE = "personal_memory_versions"


def upgrade_versions():
    if VERSIONS_TABLE in sa.inspect(op.get_bind()).get_table_names(): return
    op.create_table(
        VERSIONS_TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("agent_id", sa.String(64), sa.ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("writer", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(160)),
        sa.Column("run_id", sa.String(64)),
        sa.Column("before_hash", sa.String(64)),
        sa.Column("after_hash", sa.String(64)),
        sa.Column("before_text", sa.Text()),
        sa.Column("after_text", sa.Text()),
        sa.Column("bytes_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reverted_by", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pa_memory_versions_agent", VERSIONS_TABLE, ["agent_id", "created_at"])
    op.create_index("ix_pa_memory_versions_path", VERSIONS_TABLE, ["agent_id", "path", "created_at"])
    op.create_index("ix_pa_memory_versions_source", VERSIONS_TABLE, ["agent_id", "source_id"])


def downgrade_versions():
    op.drop_table(VERSIONS_TABLE)


USAGE_TABLE = "personal_memory_usage"


def upgrade_usage():
    if USAGE_TABLE in sa.inspect(op.get_bind()).get_table_names(): return
    op.create_table(
        USAGE_TABLE,
        sa.Column("agent_id", sa.String(64), sa.ForeignKey("personal_agents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("path", sa.String(255), primary_key=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
    )


def downgrade_usage():
    op.drop_table(USAGE_TABLE)


def upgrade():
    upgrade_versions()
    upgrade_usage()


def downgrade():
    downgrade_usage()
    downgrade_versions()
