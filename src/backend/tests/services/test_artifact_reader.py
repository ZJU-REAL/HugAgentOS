"""Unit tests for core.content.artifact_reader.

Covers caching hit/miss, ownership check, parse error path. DB is mocked
via an in-memory SQLite fixture from conftest.db_session pattern.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_artifact_row(**overrides):
    """Build a SimpleNamespace mimicking the Artifact ORM row."""
    defaults = dict(
        artifact_id="ua_test123",
        user_id="user_1",
        chat_id="chat_1",
        filename="doc.txt",
        mime_type="text/plain",
        storage_key="uploads/user_1/doc.txt",
        parsed_text=None,
        summary=None,
        parsed_at=None,
        parse_error=None,
        deleted_at=None,
        size_bytes=100,
        extra_data={"source": "user_upload"},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row
        self.committed = False

    def query(self, _model):
        return _FakeQuery(self._row)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── fetch_parsed_text ────────────────────────────────────────────────────────

def test_fetch_parsed_text_cache_hit(monkeypatch):
    from core.content import artifact_reader as ar

    row = _make_artifact_row(parsed_text="cached content")
    sess = _FakeSession(row)

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    result = ar.fetch_parsed_text("ua_test123")
    assert result == "cached content"
    assert not sess.committed  # no DB write on hit


def test_fetch_parsed_text_miss_triggers_parse_and_cache(monkeypatch):
    from core.content import artifact_reader as ar

    row = _make_artifact_row(parsed_text=None, filename="a.txt")
    sess = _FakeSession(row)

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    # Mock storage + parser
    fake_storage = SimpleNamespace(
        download_bytes=lambda key: b"raw bytes"
    )
    import core.storage as storage_mod
    monkeypatch.setattr(storage_mod, "get_storage", lambda: fake_storage)
    import core.content.file_parser as fp_mod
    monkeypatch.setattr(fp_mod, "parse_file", lambda b, name: "parsed text content")

    result = ar.fetch_parsed_text("ua_test123")
    assert result == "parsed text content"
    assert row.parsed_text == "parsed text content"
    assert row.parsed_at is not None
    assert sess.committed


def test_fetch_parsed_text_permission_denied(monkeypatch):
    from core.content import artifact_reader as ar

    row = _make_artifact_row(user_id="owner_a", parsed_text="secret")
    sess = _FakeSession(row)

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    result = ar.fetch_parsed_text("ua_test123", user_id="attacker_b")
    assert result == ""


def test_fetch_parsed_text_permission_ok(monkeypatch):
    from core.content import artifact_reader as ar

    row = _make_artifact_row(user_id="owner_a", parsed_text="ok content")
    sess = _FakeSession(row)

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    result = ar.fetch_parsed_text("ua_test123", user_id="owner_a")
    assert result == "ok content"


def test_fetch_parsed_text_deleted_artifact(monkeypatch):
    from core.content import artifact_reader as ar

    row = _make_artifact_row(parsed_text="content", deleted_at=datetime.utcnow())
    sess = _FakeSession(row)

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    result = ar.fetch_parsed_text("ua_test123")
    assert result == ""


def test_fetch_parsed_text_missing_artifact(monkeypatch):
    from core.content import artifact_reader as ar

    sess = _FakeSession(None)
    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    result = ar.fetch_parsed_text("ua_nonexistent")
    assert result == ""


def test_fetch_parsed_text_parse_error_records(monkeypatch):
    from core.content import artifact_reader as ar
    from core.infra.exceptions import StorageError

    row = _make_artifact_row(parsed_text=None)
    sess = _FakeSession(row)

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    def _raise(_key):
        raise StorageError("download", "disk err")

    fake_storage = SimpleNamespace(download_bytes=_raise)
    import core.storage as storage_mod
    monkeypatch.setattr(storage_mod, "get_storage", lambda: fake_storage)

    result = ar.fetch_parsed_text("ua_test123")
    assert result == ""
    assert row.parse_error is not None
    assert "StorageError" in row.parse_error


def test_fetch_parsed_text_empty_file_id():
    from core.content import artifact_reader as ar
    assert ar.fetch_parsed_text("") == ""
    assert ar.fetch_parsed_text(None) == ""  # type: ignore[arg-type]


# ── load_artifact_meta ───────────────────────────────────────────────────────

def test_load_artifact_meta_returns_shape(monkeypatch):
    from core.content import artifact_reader as ar

    row = _make_artifact_row(
        filename="data.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=4096,
        summary="Sheet 1: Employees",
        parsed_text="long parsed content",
    )
    sess = _FakeSession(row)
    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    meta = ar.load_artifact_meta("ua_test123")
    assert meta is not None
    assert meta["file_id"] == "ua_test123"
    assert meta["filename"] == "data.xlsx"
    assert meta["mime_type"].endswith("sheet")
    assert meta["summary"] == "Sheet 1: Employees"
    assert meta["has_parsed_text"] is True
    assert meta["source"] == "user_upload"


def test_load_artifact_meta_permission_denied(monkeypatch):
    from core.content import artifact_reader as ar
    row = _make_artifact_row(user_id="owner_a")
    sess = _FakeSession(row)
    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)
    assert ar.load_artifact_meta("ua_test123", user_id="attacker_b") is None


def test_load_artifact_meta_deleted(monkeypatch):
    from core.content import artifact_reader as ar
    row = _make_artifact_row(deleted_at=datetime.utcnow())
    sess = _FakeSession(row)
    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)
    assert ar.load_artifact_meta("ua_test123") is None


# ── infer_source ─────────────────────────────────────────────────────────────

def test_infer_source_extra_data_wins():
    from core.content.artifact_reader import (
        SOURCE_AI_GENERATED, SOURCE_USER_UPLOAD, infer_source,
    )
    assert infer_source(_make_artifact_row(
        artifact_id="ai_x", extra_data={"source": "user_upload"}
    )) == SOURCE_USER_UPLOAD
    assert infer_source(_make_artifact_row(
        artifact_id="ua_x", extra_data={"source": "ai_generated"}
    )) == SOURCE_AI_GENERATED


def test_infer_source_falls_back_to_id_prefix():
    from core.content.artifact_reader import (
        SOURCE_AI_GENERATED, SOURCE_USER_UPLOAD, infer_source,
    )
    assert infer_source(_make_artifact_row(
        artifact_id="ua_abc", extra_data={}
    )) == SOURCE_USER_UPLOAD
    assert infer_source(_make_artifact_row(
        artifact_id="chart_xyz", extra_data={}
    )) == SOURCE_AI_GENERATED
