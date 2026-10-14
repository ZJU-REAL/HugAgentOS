"""Add immutable project scope to user API keys.

Revision ID: ce_0024
Revises: ce_0023
"""

from alembic import op
import sqlalchemy as sa

revision = "ce_0024"
down_revision = "ce_0023"
branch_labels = None
depends_on = None


def upgrade():
    # CE's initial create_all may already contain the current model columns.
    bind = op.get_bind()
    existing = sa.inspect(bind)
    if "project_id" not in {col["name"] for col in existing.get_columns("user_api_keys")}:
        op.add_column("user_api_keys", sa.Column("project_id", sa.String(64), nullable=True))
    if "idx_user_api_keys_user_project" not in {
        idx["name"] for idx in existing.get_indexes("user_api_keys")
    }:
        op.create_index(
            "idx_user_api_keys_user_project", "user_api_keys", ["user_id", "project_id"]
        )
    if "ck_user_api_keys_single_scope" not in {
        check["name"] for check in existing.get_check_constraints("user_api_keys")
    }:
        with op.batch_alter_table("user_api_keys") as batch:
            batch.create_check_constraint(
                "ck_user_api_keys_single_scope", "agent_id IS NULL OR project_id IS NULL"
            )


def downgrade():
    # Never promote scoped credentials to account-wide credentials on rollback.
    op.execute(
        sa.text(
            "UPDATE user_api_keys SET enabled = false, revoked_at = CURRENT_TIMESTAMP "
            "WHERE project_id IS NOT NULL"
        )
    )
    with op.batch_alter_table("user_api_keys") as batch:
        batch.drop_constraint("ck_user_api_keys_single_scope", type_="check")
        batch.drop_index("idx_user_api_keys_user_project")
        batch.drop_column("project_id")
