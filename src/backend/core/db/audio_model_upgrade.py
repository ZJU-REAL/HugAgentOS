"""Widen audio provider types for Alembic and existing desktop SQLite databases."""
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

AUDIO_TYPES = "provider_type IN ('chat', 'embedding', 'reranker', 'audio')"


def upgrade_audio_model_type(connection):
    inspector = inspect(connection)
    if not inspector.has_table("model_providers"):
        return
    checks = inspector.get_check_constraints("model_providers")
    current = next((item for item in checks if item.get("name") == "model_providers_type_check"), None)
    if current and "'audio'" in current["sqltext"]:
        return
    if connection.dialect.name == "sqlite" and connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
        # SQLite batch recreation drops the parent table. Never cascade-delete child roles.
        raise RuntimeError("Audio schema upgrade requires SQLite foreign_keys=OFF before the migration transaction")
    operations = Operations(MigrationContext.configure(connection))
    with operations.batch_alter_table("model_providers") as batch:
        if current:
            batch.drop_constraint("model_providers_type_check", type_="check")
        batch.create_check_constraint("model_providers_type_check", AUDIO_TYPES)


def reconcile_audio_model_type(engine):
    with engine.begin() as connection:
        upgrade_audio_model_type(connection)
