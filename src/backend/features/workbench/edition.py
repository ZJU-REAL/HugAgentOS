"""Community operators must reject unknown authorization tables."""
from sqlalchemy import inspect
from core.db.engine import Base


def snapshot_permissions(db):
    names = set(inspect(db.get_bind()).get_table_names())
    if names - set(Base.metadata.tables) - {"alembic_version"}:
        raise ValueError("ce_identity_database_requires_full_edition_operator")
    return {}
