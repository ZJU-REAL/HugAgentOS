"""Preserve full capability revision and receipt IDs in content blocks.

Revision ID: ce_0017
Revises: ce_0016
"""
import sqlalchemy as sa
from alembic import op

revision = "ce_0017"
down_revision = "ce_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep existing keys stable, including longer IDs already accepted by SQLite.
    with op.batch_alter_table("content_blocks") as batch:
        batch.alter_column("id", existing_type=sa.String(64),
                           type_=sa.String(128), existing_nullable=False)


def downgrade() -> None:
    # Never truncate revision/receipt identities or silently discard history.
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM content_blocks WHERE length(id) > 64 LIMIT 1"
    )).first():
        raise RuntimeError(
            "Cannot downgrade: content_blocks contains IDs longer than 64 characters; "
            "preserve or migrate these records before retrying."
        )
    with op.batch_alter_table("content_blocks") as batch:
        batch.alter_column("id", existing_type=sa.String(128),
                           type_=sa.String(64), existing_nullable=False)
