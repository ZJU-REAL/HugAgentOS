"""Enforce personal path identity; existing duplicates require explicit maintenance."""
import sqlalchemy as sa
from alembic import op

revision = "ce_0018"
down_revision = "ce_0017"
branch_labels = None
depends_on = None

INDEXES = (
    ("uq_personal_file_folder_name", ["user_id", "user_folder_id", "filename"],
     "user_folder_id IS NOT NULL AND deleted_at IS NULL"),
    ("uq_personal_file_root_name", ["user_id", "filename"],
     "user_folder_id IS NULL AND deleted_at IS NULL"),
)


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE artifacts IN SHARE ROW EXCLUSIVE MODE"))
    table = sa.Table("artifacts", sa.MetaData(), autoload_with=connection)
    scope = ""
    duplicate = connection.execute(sa.text(
        "SELECT 1 FROM artifacts WHERE " + scope + "deleted_at IS NULL "
        "GROUP BY user_id, user_folder_id, filename HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError(
            "Personal MySpace contains duplicate names. Stop writers, back up and run "
            "scripts/clean_myspace_duplicates.py --all --apply --backup-dir <private-directory> "
            "before retrying this migration."
        )
    for name, columns, predicate in INDEXES:
        op.create_index(name, "artifacts", columns, unique=True,
                        postgresql_where=sa.text(scope + predicate),
                        sqlite_where=sa.text(scope + predicate))


def downgrade():
    for name, _, _ in reversed(INDEXES):
        op.drop_index(name, table_name="artifacts")
