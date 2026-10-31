"""The in-process session table survives a restart when a persist file is set."""

import asyncio
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from core.auth import session


@pytest.fixture()
def persisted(tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(session, "_use_memory_store", lambda: True)
    monkeypatch.setattr(session, "_persist_path", lambda: path)
    monkeypatch.setattr(session, "_STATE", session.SessionState())
    return path


def _restart(monkeypatch):
    monkeypatch.setattr(session, "_STATE", session.SessionState())


def test_session_survives_restart(persisted, monkeypatch):
    token = asyncio.run(session.create_session({"user_id": "u1", "username": "admin"}))

    _restart(monkeypatch)
    user = asyncio.run(session.validate_session(token))

    assert user is not None and user["user_id"] == "u1"


def test_persist_file_holds_digests_only_and_is_private(persisted):
    token = asyncio.run(session.create_session({"user_id": "u1"}))

    text = persisted.read_text(encoding="utf-8")
    assert token not in text
    assert session._hash_token(token) in json.loads(text)
    if os.name == "posix":
        assert stat.S_IMODE(persisted.stat().st_mode) == 0o600


def test_revoked_session_stays_revoked_after_restart(persisted, monkeypatch):
    token = asyncio.run(session.create_session({"user_id": "u1"}))
    assert asyncio.run(session.revoke_session(token)) is True

    _restart(monkeypatch)

    assert asyncio.run(session.validate_session(token)) is None


def test_expired_and_malformed_entries_are_dropped_on_load(persisted, monkeypatch):
    now = datetime.now(timezone.utc)
    persisted.write_text(
        json.dumps(
            {
                "expired": {"payload": {"user_id": "old"}, "expires_at": (now - timedelta(minutes=1)).isoformat()},
                "broken": {"payload": {"user_id": "x"}},
                "live": {"payload": {"user_id": "live"}, "expires_at": (now + timedelta(hours=1)).isoformat()},
            }
        ),
        encoding="utf-8",
    )

    assert set(session._memory_sessions()) == {"live"}


def test_unreadable_file_starts_empty(persisted):
    persisted.write_text("{not json", encoding="utf-8")

    assert session._memory_sessions() == {}
    token = asyncio.run(session.create_session({"user_id": "u1"}))
    assert session._hash_token(token) in json.loads(persisted.read_text(encoding="utf-8"))


def test_without_persist_path_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_use_memory_store", lambda: True)
    monkeypatch.setattr(session, "_persist_path", lambda: None)
    monkeypatch.setattr(session, "_STATE", session.SessionState())
    monkeypatch.chdir(tmp_path)

    asyncio.run(session.create_session({"user_id": "u1"}))

    assert list(tmp_path.iterdir()) == []
