import time

import pytest
from core.myspace import mirror
from core.space_sync import personal as watcher
from tests.myspace.test_registry_watcher import registry, _stub_present, _capture_registrations


@pytest.mark.asyncio
async def test_failed_registration_remains_pending_then_retries(registry, monkeypatch):
    _stub_present(monkeypatch, mirror.VERDICT_NEW)
    monkeypatch.setattr(mirror, "register_entry", lambda **kwargs: None)
    registry._pending[("u1", "retry.txt")] = time.monotonic()
    with pytest.raises(RuntimeError, match="同步未完成"):
        await registry.flush("u1", timeout=0.15)
    assert ("u1", "retry.txt") in registry._pending
    assert ("u1", "retry.txt") in registry._deferred
    seen = _capture_registrations(monkeypatch)
    await registry.flush("u1")
    assert seen == [("u1", "retry.txt")]
    assert not registry._pending


@pytest.mark.asyncio
async def test_failed_claim_batch_can_be_reclaimed(monkeypatch):
    from core.infra.ephemeral import LocalEphemeralState
    from core.myspace.registry_claims import claim, claim_batch
    state = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: state)
    with pytest.raises(RuntimeError):
        async with claim_batch():
            assert await claim("u", "file", "stamp")
            raise RuntimeError("storage failed")
    async with claim_batch():
        assert await claim("u", "file", "stamp")
    async with claim_batch():
        assert not await claim("u", "file", "stamp")


@pytest.mark.asyncio
async def test_delayed_delete_does_not_delete_recreated_identity(registry, monkeypatch):
    from tests.myspace.test_registry_watcher import _stub_gone
    target = mirror.DeleteTarget(registered=mirror.RegisteredFile(
        artifact_id="recreated", storage_key="new-key", registered_ts=20.0))
    _stub_gone(monkeypatch, target)
    registry._processing_times[("u1", "file.txt")] = 10.0

    def must_not_delete(**kwargs):
        raise AssertionError("old event deleted new identity")

    monkeypatch.setattr(mirror, "delete_registered", must_not_delete)
    await registry._apply_deletes("u1", ["file.txt"], watcher.Ask("u1"))


@pytest.mark.asyncio
async def test_read_events_do_not_queue_registration(registry):
    import asyncio
    from types import SimpleNamespace
    registry._loop = asyncio.get_running_loop()
    for kind in ("opened", "closed_no_write"):
        registry._on_event(SimpleNamespace(event_type=kind, is_directory=False,
                          src_path=str(registry._root / "u1" / "file.txt")))
    await asyncio.sleep(0)
    assert not registry._pending


@pytest.mark.asyncio
async def test_large_batch_does_not_exhaust_claim_pool(registry, monkeypatch):
    import asyncio
    _stub_present(monkeypatch, mirror.VERDICT_CURRENT)
    active = peak = 0

    async def limited_claim(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if active > 8:
                raise RuntimeError("Too many connections")
            await asyncio.sleep(0)
            return True
        finally:
            active -= 1

    monkeypatch.setattr(watcher, "_claim", limited_claim)
    await registry._process("u1", [f"{i}.txt" for i in range(600)], force=True)
    assert peak <= 8
    assert active == 0


@pytest.mark.asyncio
async def test_listing_sync_failure_is_reported_as_pending(monkeypatch):
    from types import SimpleNamespace

    async def unavailable(user_id):
        raise RuntimeError("storage temporarily unavailable")

    from core.space_sync import personal_registry
    from fastapi import HTTPException
    monkeypatch.setattr(personal_registry, "_registry", SimpleNamespace(flush=unavailable))
    with pytest.raises(HTTPException) as error:
        await personal_registry.flush_user("u1")
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_sandbox_sync_failure_does_not_block_command(monkeypatch):
    from core.llm.tools.sandbox_tool import _pull_myspace_updates

    def unavailable(**kwargs):
        raise RuntimeError("storage temporarily unavailable")

    monkeypatch.setattr("core.myspace.projection.pull_myspace_updates", unavailable)
    await _pull_myspace_updates("u1")
