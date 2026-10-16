"""Shared EE/CE and desktop migration for exclusive plugin-resource ownership."""

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, String, inspect, text

OWNER = (
    "(chat_id IS NOT NULL AND chat_id <> '' AND application_key IS NULL AND instance_id IS NULL) OR "
    "(chat_id IS NULL AND application_key IS NOT NULL AND application_key <> '' AND instance_id IS NOT NULL AND instance_id <> '')"
)


def upgrade_resource_owners(connection):
    """Preserve old conversation rows; enforce one exact application or chat owner."""
    inspector = inspect(connection)
    columns = {c["name"]: c for c in inspector.get_columns("plugin_resources")}
    checks = {c["name"] for c in inspector.get_check_constraints("plugin_resources")}
    indexes = {i["name"] for i in inspector.get_indexes("plugin_resources")}
    operations = Operations(MigrationContext.configure(connection))
    with operations.batch_alter_table("plugin_resources") as batch:
        if not columns["chat_id"]["nullable"]:
            batch.alter_column("chat_id", existing_type=String(128), nullable=True)
        for name, size in (("application_key", 256), ("instance_id", 128), ("request_digest", 64)):
            if name not in columns:
                batch.add_column(Column(name, String(size), nullable=True))
        if "ck_plugin_resource_owner" not in checks:
            batch.create_check_constraint("ck_plugin_resource_owner", OWNER)
        if "ix_plugin_resources_application_key" not in indexes:
            batch.create_index("ix_plugin_resources_application_key", ["application_key"])


def downgrade_resource_owners(connection):
    """Refuse data loss before touching schema; old code cannot own app resources."""
    if connection.execute(
        text("SELECT count(*) FROM plugin_resources WHERE application_key IS NOT NULL")
    ).scalar():
        raise RuntimeError("Export application resource records before downgrade")
    operations = Operations(MigrationContext.configure(connection))
    with operations.batch_alter_table("plugin_resources") as batch:
        batch.drop_index("ix_plugin_resources_application_key")
        batch.drop_constraint("ck_plugin_resource_owner", type_="check")
        batch.drop_column("request_digest")
        batch.drop_column("instance_id")
        batch.drop_column("application_key")
        batch.alter_column("chat_id", existing_type=String(128), nullable=False)


def reconcile_resource_owners(engine):
    """Upgrade desktop SQLite before generic startup column reconciliation."""
    with engine.begin() as connection:
        if "plugin_resources" not in inspect(connection).get_table_names():
            return
        upgrade_resource_owners(connection)
