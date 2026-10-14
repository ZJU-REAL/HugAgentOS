"""Allow genuinely empty edited text artifacts; preserve existing bytes."""

from alembic import op
import sqlalchemy as sa

revision = "ce_0023"
down_revision = "ce_0022"
branch_labels = None
depends_on = None


def _change(expression):
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_constraint("artifacts_size_check", type_="check")
        batch.create_check_constraint("artifacts_size_check", expression)


def upgrade():
    _change("size_bytes >= 0")


def downgrade():
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM artifacts WHERE size_bytes = 0"))
        .scalar()
    )
    if count:
        raise RuntimeError("Restore or remove empty artifacts before downgrade; no data is changed")
    _change("size_bytes > 0")
