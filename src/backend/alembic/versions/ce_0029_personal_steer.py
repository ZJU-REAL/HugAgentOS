"""Remove task input versions; steer consumes actual user messages."""
from alembic import op
import sqlalchemy as sa

revision = "ce_0029"
down_revision = "ce_0028"
branch_labels = None
depends_on = None


def upgrade():
    for table, column in (("personal_work_items", "revision"),
                          ("personal_session_operations", "task_revision")):
        inspector = sa.inspect(op.get_bind())
        if table in inspector.get_table_names() and column in {c["name"] for c in inspector.get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "personal_work_items" in tables:
        with op.batch_alter_table("personal_work_items") as batch:
            batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    if "personal_session_operations" in tables:
        with op.batch_alter_table("personal_session_operations") as batch:
            batch.add_column(sa.Column("task_revision", sa.Integer(), nullable=True))
