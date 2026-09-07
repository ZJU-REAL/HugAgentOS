"""File-content CAS and credential transactions use only temporary config/secret data."""

from __future__ import annotations

import hashlib
import json

import pytest
from core.capabilities import credentials, mcp_json


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch, caps_root):
    monkeypatch.setenv("HUGAGENT_CREDENTIAL_BACKEND", "memory")
    credentials._memory_store.clear()
    yield
    credentials._memory_store.clear()


def add_http(secret="test-one"):
    return mcp_json.upsert_local_server(
        "service",
        {"transport": "streamable_http", "url": "https://example.invalid/mcp"},
        secret_headers={"Authorization": secret},
    )


def fail_replace(*_args):
    raise OSError("injected atomic replacement failure")


def test_external_edit_without_generation_change_is_preserved(caps_root):
    add_http()
    stale = mcp_json.load()
    path = caps_root / "mcp.json"
    manual = json.loads(path.read_text())
    manual["local"]["servers"]["manual"] = {"transport": "stdio", "command": "python"}
    manual["user_extension"] = {"preserve": True}
    raw = json.dumps(manual, indent=4) + "\n"
    path.write_text(raw)
    stale.managed["p_one"] = {"servers": {}}
    with pytest.raises(mcp_json.McpJsonConflict, match="content"):
        mcp_json.write(stale, expected_generation=stale.generation)
    assert path.read_text() == raw


def test_digest_tracks_actual_bytes_and_successful_commit(caps_root):
    assert mcp_json.load().digest == ""
    doc = add_http()
    raw = (caps_root / "mcp.json").read_bytes()
    assert doc.digest == mcp_json.load().digest == hashlib.sha256(raw).hexdigest()
    assert "digest" not in json.loads(raw)


def test_failed_write_does_not_advance_in_memory_snapshot(caps_root, monkeypatch):
    doc = add_http()
    generation, digest = doc.generation, doc.digest
    original = (caps_root / "mcp.json").read_bytes()
    monkeypatch.setattr(mcp_json.os, "replace", fail_replace)
    with pytest.raises(OSError):
        mcp_json.write(doc, expected_generation=generation)
    assert (doc.generation, doc.digest) == (generation, digest)
    assert (caps_root / "mcp.json").read_bytes() == original


def test_remove_write_failure_keeps_referenced_secret(caps_root, monkeypatch):
    add_http()
    before = dict(credentials._memory_store)
    original = (caps_root / "mcp.json").read_bytes()
    monkeypatch.setattr(mcp_json.os, "replace", fail_replace)
    with pytest.raises(OSError):
        mcp_json.remove_local_server("service")
    assert credentials._memory_store == before
    assert (caps_root / "mcp.json").read_bytes() == original
    assert mcp_json.local_server_configs()["service"]["headers"] == {"Authorization": "test-one"}


def test_replacing_secret_write_failure_keeps_old_secret(caps_root, monkeypatch):
    add_http()
    before = dict(credentials._memory_store)
    original = (caps_root / "mcp.json").read_bytes()
    monkeypatch.setattr(mcp_json.os, "replace", fail_replace)
    with pytest.raises(OSError):
        add_http("test-two")
    assert credentials._memory_store == before
    assert (caps_root / "mcp.json").read_bytes() == original


def test_config_edit_without_new_secret_keeps_existing_reference(caps_root):
    before = add_http()
    ref = before.local["service"]["credentialRef"]
    doc = mcp_json.upsert_local_server(
        "service",
        {"transport": "streamable_http", "url": "https://example.invalid/changed"},
        expected_generation=before.generation,
        expected_digest=before.digest,
    )
    assert doc.local["service"]["credentialRef"] == ref
    assert mcp_json.local_server_configs()["service"]["headers"] == {"Authorization": "test-one"}


def test_explicit_stale_digest_rejects_before_secret_changes(caps_root):
    stale = add_http()
    mcp_json.upsert_local_server("manual", {"transport": "stdio", "command": "python"})
    current = mcp_json.load()
    before = dict(credentials._memory_store)
    original = (caps_root / "mcp.json").read_bytes()
    with pytest.raises(mcp_json.McpJsonConflict, match="content"):
        mcp_json.upsert_local_server(
            "service",
            {"transport": "streamable_http", "url": "https://example.invalid/changed"},
            secret_headers={"Authorization": "test-two"},
            expected_generation=current.generation,
            expected_digest=stale.digest,
        )
    assert credentials._memory_store == before
    assert (caps_root / "mcp.json").read_bytes() == original
    with pytest.raises(mcp_json.McpJsonConflict):
        mcp_json.remove_local_server(
            "service", expected_generation=current.generation, expected_digest=stale.digest
        )


def test_successful_replacement_cleans_old_secret_only_after_commit(caps_root):
    old_ref = add_http().local["service"]["credentialRef"]
    new_ref = add_http("test-two").local["service"]["credentialRef"]
    assert old_ref != new_ref
    assert credentials.load_secret(old_ref) is None
    assert credentials.load_headers(new_ref) == {"Authorization": "test-two"}
    assert mcp_json.remove_local_server("service")
    assert credentials.load_secret(new_ref) is None


def test_removing_one_of_two_servers_preserves_shared_credential(caps_root):
    ref = add_http().local["service"]["credentialRef"]
    mcp_json.upsert_local_server(
        "other",
        {
            "transport": "streamable_http",
            "url": "https://example.invalid/other",
            "credentialRef": ref,
        },
    )
    assert mcp_json.remove_local_server("service")
    assert credentials.load_headers(ref) == {"Authorization": "test-one"}
    assert mcp_json.local_server_configs()["other"]["headers"] == {"Authorization": "test-one"}


def test_invalid_edit_does_not_overwrite_existing_secret(caps_root):
    add_http()
    before = dict(credentials._memory_store)
    with pytest.raises(mcp_json.McpJsonError):
        mcp_json.upsert_local_server(
            "service", {"transport": "invalid"}, secret_headers={"Authorization": "test-two"}
        )
    assert credentials._memory_store == before


def test_external_edit_while_temp_file_is_written_is_preserved(caps_root, monkeypatch):
    stale = add_http()
    path = caps_root / "mcp.json"
    original = json.loads(path.read_text())
    original["external_extension"] = "edited during save"
    edited = json.dumps(original) + "\n"
    fsync = mcp_json.os.fsync

    def edit_on_flush(fd):
        fsync(fd)
        path.write_text(edited)

    monkeypatch.setattr(mcp_json.os, "fsync", edit_on_flush)
    with pytest.raises(mcp_json.McpJsonConflict, match="content"):
        mcp_json.write(stale, expected_generation=stale.generation)
    assert path.read_text() == edited
    assert not list(caps_root.glob(".mcp.json.*.tmp"))


def test_file_created_since_empty_snapshot_is_not_overwritten(caps_root):
    stale = mcp_json.load()
    caps_root.mkdir(parents=True, exist_ok=True)
    path = caps_root / "mcp.json"
    manual = mcp_json.McpJson().to_dict()
    manual["local"]["servers"]["manual"] = {"transport": "stdio", "command": "python"}
    raw = json.dumps(manual)
    path.write_text(raw)
    with pytest.raises(mcp_json.McpJsonConflict, match="content"):
        mcp_json.write(stale, expected_generation=0)
    assert path.read_text() == raw
