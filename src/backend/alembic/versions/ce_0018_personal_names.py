"""Enforce personal path identity using the canonical CE model indexes."""
import sqlalchemy as sa
from alembic import op

from core.db.models import Artifact
from core.db.personal_file_names import INDEX_NAMES

revision = "ce_0018"
down_revision = "ce_0017"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE artifacts IN SHARE ROW EXCLUSIVE MODE"))
    duplicate = connection.execute(sa.text(
        "SELECT 1 FROM artifacts WHERE deleted_at IS NULL "
        "GROUP BY user_id, user_folder_id, filename HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError(
            "Personal MySpace contains duplicate names. Stop writers, back up and run "
            "scripts/clean_myspace_duplicates.py --all --apply --backup-dir <private-directory> "
            "before retrying this migration."
        )
    # CE's baseline creates current model metadata, including these indexes.
    # Keep one index definition and let SQLAlchemy inspect before emitting DDL.
    indexes = {index.name: index for index in Artifact.__table__.indexes}
    for name in INDEX_NAMES:
        indexes[name].create(bind=connection, checkfirst=True)


def downgrade():
    indexes = {index.name: index for index in Artifact.__table__.indexes}
    for name in reversed(INDEX_NAMES):
        indexes[name].drop(bind=op.get_bind())
