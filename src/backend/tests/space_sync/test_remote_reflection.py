"""Bounded remote fetches retain their cursor and deletion baseline across restart."""

import hashlib
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from core.myspace import sandbox_sync as sync


@pytest.mark.asyncio
async def test_file_201_failure_and_restart_deletion(tmp_path, monkeypatch):
    root = tmp_path / "myspace_cache" / "u"
    root.mkdir(parents=True)
    monkeypatch.setattr("core.sandbox._common.myspace_cache_dir", lambda user: root)
    monkeypatch.setattr(sync, "_last_reflect", OrderedDict())
    names = {f"{i:03}.txt" for i in range(201)}
    digest = hashlib.md5(b"bytes").hexdigest()
    cursors = []

    async def listing(*args, since_ts=None):
        cursors.append(since_ts)
        return {name: digest for name in names}, set(names)

    failed_once = True
    from core.sandbox import SandboxError

    async def get_file(session, path, **kwargs):
        nonlocal failed_once
        if path.endswith("000.txt") and failed_once:
            failed_once = False
            raise SandboxError("temporary download failure")
        return b"bytes"

    monkeypatch.setattr(sync, "_list_sandbox_myspace", listing)
    monkeypatch.setattr(
        "core.sandbox.get_sandbox_provider",
        lambda: SimpleNamespace(myspace_mirror_live=False, get_file=get_file),
    )
    await sync.reflect_sandbox_myspace(session_id="session", user_id="u")
    assert len(list(root.glob("*.txt"))) == 199
    await sync.reflect_sandbox_myspace(session_id="session", user_id="u")
    assert len(list(root.glob("*.txt"))) == 201
    assert cursors[:2] == [None, None]
    # Lose the process-local cache and recover the persisted sandbox baseline.
    sync._last_reflect.clear()
    names.remove("200.txt")
    await sync.reflect_sandbox_myspace(session_id="session", user_id="u")
    assert not (root / "200.txt").exists()
    assert len(list(root.glob("*.txt"))) == 200


@pytest.mark.asyncio
async def test_remote_metadata_uses_ctime_for_preserved_mtime(monkeypatch):
    commands = []

    async def execute(command, **kwargs):
        commands.append(command)
        return 0, sync._LIST_SEPARATOR + "\n", ""

    monkeypatch.setattr("core.llm.tools._common.sandbox_exec_bash", execute)
    assert await sync._list_sandbox_myspace("s", "u", since_ts=100) == ({}, set())
    assert "-newerct @100" in commands[0]


@pytest.mark.asyncio
async def test_remote_deletion_rejects_parent_symlink_and_retains_retry(tmp_path, monkeypatch):
    from core.space_sync.index import reflection

    root = tmp_path / "myspace_cache" / "u"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "protected.txt").write_bytes(b"protected")
    (root / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("core.sandbox._common.myspace_cache_dir", lambda user: root)
    monkeypatch.setattr(sync, "_last_reflect", OrderedDict())

    async def listing(*args, **kwargs):
        return {}, set()

    monkeypatch.setattr(sync, "_list_sandbox_myspace", listing)
    monkeypatch.setattr(
        "core.sandbox.get_sandbox_provider", lambda: SimpleNamespace(myspace_mirror_live=False)
    )
    reflection("u", "s", [None, ["link/protected.txt"]])
    # Session key is inspected directly to retain the exact prior baseline.
    sync._last_reflect["s"] = (None, frozenset({"link/protected.txt"}))
    await sync.reflect_sandbox_myspace(session_id="s", user_id="u")
    assert (outside / "protected.txt").read_bytes() == b"protected"
    assert "link/protected.txt" in sync._last_reflect["s"][1]
