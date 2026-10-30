"""Remove domain ontology governance and delete its persisted data.

Revision ID: ce_0035
Revises: ce_0034

Back up the database before upgrading. Data rollback requires restoring that
backup; conversations and every other feature are retained.
"""
from alembic import op

from core.db.ontology_retirement import retire_ontology

revision = "ce_0035"
down_revision = "ce_0034"
branch_labels = None
depends_on = None


def upgrade():
    retire_ontology(op.get_bind())


def downgrade():
    raise RuntimeError(
        "Ontology data was deleted. Restore the pre-upgrade database backup "
        "together with the previous application version to roll back."
    )
