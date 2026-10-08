"""Retired transport removal and archived migration round trips."""

import pytest
from sqlalchemy import inspect
from tests.api.test_application_hosting import hosted


def test_retired_storage_routes_are_absent(hosted):
    from api.routes import sites_serve
    from api.routes.v1 import internal_sites, sites

    client, _ = hosted
    client.app.include_router(sites.router)
    client.app.include_router(internal_sites.router)
    client.app.include_router(sites_serve.router)
    for method, path in [
        ("GET", "/v1/sites/retired/kv"),
        ("GET", "/v1/sites/retired/submissions"),
        ("POST", "/v1/internal/sites/kv"),
        ("GET", "/site/retired/__api/kv/test"),
        ("POST", "/site/retired/__api/forms/contact"),
    ]:
        assert client.request(method, path, json={}).status_code in (404, 405)


def test_retirement_migration_archives_and_restores(hosted, tmp_path, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, insert, select

    _, engine = hosted
    from datetime import datetime, timezone

    timestamp = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    metadata = MetaData()
    Table("sites", metadata, Column("site_id", String(64), primary_key=True))
    kv = Table(
        "site_kv",
        metadata,
        Column("site_id", String(64), primary_key=True),
        Column("k", String(64), primary_key=True),
        Column("v", String),
        Column("updated_at", DateTime(timezone=True)),
    )
    forms = Table(
        "site_submissions",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("site_id", String(64)),
        Column("form_key", String),
        Column("payload", JSON),
        Column("client_ip", String),
        Column("created_at", DateTime(timezone=True)),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(metadata.tables["sites"]), {"site_id": "old-site"})
        connection.execute(
            insert(kv),
            {"site_id": "old-site", "k": "settings", "v": "original", "updated_at": timestamp},
        )
        connection.execute(
            insert(forms),
            {
                "id": "form1",
                "site_id": "old-site",
                "form_key": "contact",
                "payload": {"message": "original"},
                "created_at": timestamp,
            },
        )
    path = Path(__file__).parents[2] / "alembic/versions/site05retire_remove_legacy_storage.py"
    spec = importlib.util.spec_from_file_location("retirement_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    monkeypatch.setenv("SITE_STORAGE_BACKUP_DIR", str(tmp_path))
    from core.db.site_storage_retirement import _digest

    with engine.connect() as connection:
        original_rows = {
            name: [dict(row) for row in connection.execute(select(table)).mappings()]
            for name, table in (("site_kv", kv), ("site_submissions", forms))
        }
    payload = json.loads(json.dumps(original_rows, default=lambda value: value.isoformat()))
    with pytest.raises(RuntimeError, match="Complete and verify"):
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
    receipt_dir = tmp_path / "kv-to-sql-test"
    receipt_dir.mkdir()
    (receipt_dir / "receipt.json").write_text(
        json.dumps(
            {
                "verified": True,
                "legacy_sha256": _digest(payload),
                "sites": [{"site_id": "old-site"}],
            }
        )
    )
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
    assert not inspect(engine).has_table("site_kv")
    assert not inspect(engine).has_table("site_submissions")
    archive = next(tmp_path.glob("site-storage-*.json"))
    assert archive.stat().st_mode & 0o777 == 0o600
    snapshot = json.loads(archive.read_text())
    assert snapshot["tables"]["site_kv"][0]["v"] == "original"
    monkeypatch.setenv("SITE_STORAGE_RESTORE_FILE", str(archive))
    original = archive.read_text()
    snapshot["tables"]["site_kv"][0]["v"] = "tampered"
    archive.write_text(json.dumps(snapshot))
    with pytest.raises(RuntimeError, match="archive is invalid"):
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision.downgrade()
    assert not inspect(engine).has_table("site_kv")
    assert not inspect(engine).has_table("site_submissions")
    archive.write_text(original)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
    with engine.connect() as connection:
        assert connection.scalar(select(kv.c.v)) == "original"
        assert connection.scalar(select(forms.c.payload)) == {"message": "original"}

    types = {
        column["name"]: column["type"] for column in inspect(engine).get_columns("site_submissions")
    }
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB

        assert isinstance(types["payload"], JSONB)
    restored = Table("site_kv", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        value = connection.scalar(select(restored.c.updated_at))
        assert value == (
            timestamp if engine.dialect.name == "postgresql" else timestamp.replace(tzinfo=None)
        )
