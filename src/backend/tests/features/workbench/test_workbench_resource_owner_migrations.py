"""Real CE resource-owner migration on isolated SQLite and PostgreSQL tables."""
import importlib.util
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

SOURCE = Path(os.environ.get("WORKBENCH_OWNER_SOURCE_ROOT", Path(__file__).resolve().parents[5]))


def migration():
    path = SOURCE / "ce/overlay/src/backend/alembic/versions/ce_0026_workbench_resource_owner.py"
    if not path.is_file():
        path = SOURCE / "src/backend/alembic/versions/ce_0026_workbench_resource_owner.py"
    spec = importlib.util.spec_from_file_location("ce_owner_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "ce_0026"
    assert module.down_revision == "ce_0025"
    return module


@pytest.fixture(params=["sqlite", "postgresql"])
def connection(request, tmp_path):
    if request.param == "postgresql":
        url = os.environ.get("WORKBENCH_OWNER_TEST_PG_URL")
        if not url:
            pytest.skip("isolated WORKBENCH_OWNER_TEST_PG_URL required for PostgreSQL")
        assert "127.0.0.1" in url and "workbench_owner_probe" in url
    else:
        url = "sqlite:///" + str(tmp_path / "owner.sqlite")
    engine = sa.create_engine(url)
    with engine.begin() as bind:
        bind.execute(sa.text("CREATE TABLE plugin_resources (resource_id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(128) NOT NULL, status VARCHAR(16) NOT NULL)"))
        bind.execute(sa.text("INSERT INTO plugin_resources VALUES ('chat-original', 'chat-original-id', 'active')"))
    with engine.connect() as bind:
        try:
            yield bind
        finally:
            bind.rollback()
            bind.execute(sa.text("DROP TABLE plugin_resources"))
            bind.commit()
    engine.dispose()


def invoke(bind, method):
    with Operations.context(MigrationContext.configure(bind)):
        getattr(migration(), method)()
    bind.commit()


def test_upgrade_preserves_chat_and_rejects_invalid_owners(connection):
    invoke(connection, "upgrade")
    inspector = sa.inspect(connection)
    columns = {c["name"]: c for c in inspector.get_columns("plugin_resources")}
    assert columns["chat_id"]["nullable"] is True
    assert {"application_key", "instance_id", "request_digest"} <= columns.keys()
    assert columns["application_key"]["type"].length == 256
    assert columns["instance_id"]["type"].length == 128
    assert columns["request_digest"]["type"].length == 64
    assert "ck_plugin_resource_owner" in {c["name"] for c in inspector.get_check_constraints("plugin_resources")}
    assert "ix_plugin_resources_application_key" in {i["name"] for i in inspector.get_indexes("plugin_resources")}
    assert connection.execute(sa.text("SELECT chat_id FROM plugin_resources WHERE resource_id='chat-original'")).scalar() == "chat-original-id"
    connection.execute(sa.text("INSERT INTO plugin_resources (resource_id, chat_id, status, application_key, instance_id) VALUES ('app-valid', NULL, 'active', 'plugin:probe', 'instance-valid')"))
    for n, chat, app, instance in [(1, None, None, None), (2, "chat", "plugin:probe", "instance"), (3, None, "", "instance"), (4, None, "plugin:probe", ""), (5, "", None, None)]:
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(sa.text("INSERT INTO plugin_resources (resource_id, chat_id, status, application_key, instance_id) VALUES (:id, :chat, 'active', :app, :instance)"), {"id": str(n), "chat": chat, "app": app, "instance": instance})
    connection.commit()
    invoke(connection, "upgrade")  # repeated upgrade preserves the same constraint/index
    assert connection.execute(sa.text("SELECT count(*) FROM plugin_resources")).scalar() == 2


def test_downgrade_refuses_application_data_loss(connection):
    invoke(connection, "upgrade")
    connection.execute(sa.text("INSERT INTO plugin_resources (resource_id, chat_id, status, application_key, instance_id) VALUES ('app-valid', NULL, 'active', 'plugin:probe', 'instance-valid')"))
    connection.commit()
    with pytest.raises(RuntimeError, match="Export application resource records"):
        invoke(connection, "downgrade")
    connection.rollback()
    assert connection.execute(sa.text("SELECT count(*) FROM plugin_resources")).scalar() == 2
    assert "application_key" in {c["name"] for c in sa.inspect(connection).get_columns("plugin_resources")}


def test_downgrade_without_application_rows_is_safe(connection):
    invoke(connection, "upgrade")
    invoke(connection, "downgrade")
    inspector = sa.inspect(connection)
    columns = {c["name"]: c for c in inspector.get_columns("plugin_resources")}
    assert columns["chat_id"]["nullable"] is False
    assert not {"application_key", "instance_id", "request_digest"} & columns.keys()
    assert "ix_plugin_resources_application_key" not in {i["name"] for i in inspector.get_indexes("plugin_resources")}
    assert connection.execute(sa.text("SELECT chat_id FROM plugin_resources")).scalar() == "chat-original-id"
