"""Data retirement must delete Batch state without deleting shared features."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from core.db.batch_retirement import retire_batch_execution

JSON = sa.JSON


@pytest.fixture
def database():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    specs = {
        "batch_plans": [sa.Column("plan_id", sa.String, primary_key=True)],
        "admin_mcp_servers": [sa.Column("server_id", sa.String, primary_key=True)],
        "catalog_overrides": [sa.Column("id", sa.Integer, primary_key=True),
                              sa.Column("item_id", sa.String)],
        "chat_sessions": [sa.Column("chat_id", sa.String, primary_key=True),
                          sa.Column("metadata", JSON)],
        "chat_messages": [sa.Column("message_id", sa.String, primary_key=True),
                          sa.Column("tool_calls", JSON)],
        "roles": [sa.Column("role_id", sa.String, primary_key=True),
                  sa.Column("permissions", JSON)],
        "scheduled_tasks": [sa.Column("task_id", sa.String, primary_key=True),
                            sa.Column("enabled_mcp_ids", JSON)],
        "content_blocks": [sa.Column("id", sa.String, primary_key=True),
                           sa.Column("payload", JSON)],
        "chat_runs": [sa.Column("run_id", sa.String, primary_key=True),
                      sa.Column("request_payload", JSON)],
    }
    tables = {name: sa.Table(name, metadata, *columns) for name, columns in specs.items()}
    metadata.create_all(engine)
    yield engine, tables
    engine.dispose()


def test_deletes_retired_data_preserves_shared_records_and_is_idempotent(database):
    engine, tables = database
    with engine.begin() as bind:
        bind.execute(tables["batch_plans"].insert(), {"plan_id": "retired"})
        bind.execute(tables["admin_mcp_servers"].insert(),
                     [{"server_id": "batch_runner"}, {"server_id": "web_fetch"}])
        bind.execute(tables["catalog_overrides"].insert(),
                     [{"id": 1, "item_id": "batch_runner"}, {"id": 2, "item_id": "web_fetch"}])
        bind.execute(tables["chat_sessions"].insert(), {
            "chat_id": "ordinary-history",
            "metadata": {"batch_chat": True, "batch_mode_active": True,
                         "workflow_chat": True, "title_manually_set": True},
        })
        bind.execute(tables["chat_messages"].insert(), {
            "message_id": "keep-history", "tool_calls": [
                {"tool_name": "batch_plan", "result": {"plan_id": "retired"}},
                {"tool_name": "web_fetch", "result": {"text": "keep"}},
            ],
        })
        bind.execute(tables["roles"].insert(), {
            "role_id": "user", "permissions": {"allowed_apps": ["plan_mode", "batch_runner"]},
        })
        bind.execute(tables["scheduled_tasks"].insert(), {
            "task_id": "daily", "enabled_mcp_ids": ["batch_runner", "web_fetch"],
        })
        bind.execute(tables["content_blocks"].insert(), {
            "id": "app_config", "payload": {"apps": [{"id": "batch_runner"}, {"id": "custom"}]},
        })
        bind.execute(tables["chat_runs"].insert(), [
            {"run_id": "batch-attempt", "request_payload": {"kind": "batch_item"}},
            {"run_id": "ordinary-run", "request_payload": {"kind": "stream", "message": "keep"}},
        ])
        result = retire_batch_execution(bind)
        assert result["batch_plans"] == 1
        assert not sa.inspect(bind).has_table("batch_plans")
        assert list(bind.execute(sa.select(tables["admin_mcp_servers"].c.server_id)).scalars()) == ["web_fetch"]
        assert list(bind.execute(sa.select(tables["catalog_overrides"].c.item_id)).scalars()) == ["web_fetch"]
        assert bind.execute(sa.select(tables["chat_sessions"].c.metadata)).scalar_one() == {
            "workflow_chat": True, "title_manually_set": True,
        }
        assert bind.execute(sa.select(tables["chat_messages"].c.tool_calls)).scalar_one() == [
            {"tool_name": "web_fetch", "result": {"text": "keep"}},
        ]
        assert bind.execute(sa.select(tables["roles"].c.permissions)).scalar_one() == {
            "allowed_apps": ["plan_mode"],
        }
        assert bind.execute(sa.select(tables["scheduled_tasks"].c.enabled_mcp_ids)).scalar_one() == ["web_fetch"]
        assert bind.execute(sa.select(tables["content_blocks"].c.payload)).scalar_one() == {
            "apps": [{"id": "custom"}],
        }
        assert list(bind.execute(sa.select(tables["chat_runs"].c.run_id)).scalars()) == ["ordinary-run"]
        assert not any(retire_batch_execution(bind).values())


def test_empty_database_is_safe_and_downgrade_requires_backup():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as bind:
        assert retire_batch_execution(bind) == {}
    path = Path(__file__).parents[1] / "alembic/versions/retirebatch01_remove_batch_execution.py"
    spec = importlib.util.spec_from_file_location("retirement", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "projectkey01"
    with pytest.raises(RuntimeError, match="backup"):
        migration.downgrade()
    engine.dispose()
