"""Shared app initialization stays singular and reset cancels its task."""
import asyncio
import pytest
from integrations.lark import configuration, state, storage
from integrations.lark.service import LarkService


@pytest.mark.asyncio
async def test_pending_app_is_shared_and_reset_cancels_task(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "APP_INIT_TASK", None)
    monkeypatch.setattr(state, "app_init_state", {"status": "idle"})
    monkeypatch.setattr(configuration, "app_configured", lambda: None)
    home = tmp_path / "app-home"
    home.mkdir()
    (home / "config.json").write_text("{}")
    monkeypatch.setattr(storage, "lark_app_home_dir", lambda: home)
    calls = []

    async def initialize():
        calls.append("start")
        state.app_init_state.update(status="pending", verification_url="https://example.test/setup")
        await asyncio.Future()

    monkeypatch.setattr(configuration, "app_init_flow", initialize)
    service = LarkService()
    try:
        result = await service.start_app_init()
        task = state.APP_INIT_TASK
        assert result["verification_url"] == "https://example.test/setup"
        assert await LarkService().start_app_init() == result
        assert state.APP_INIT_TASK is task and calls == ["start"]
        reset = await service.reset_app()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reset["status"] == "idle" and state.APP_INIT_TASK is None
        assert not home.exists()
    finally:
        if state.APP_INIT_TASK is not None:
            state.APP_INIT_TASK.cancel()


@pytest.mark.asyncio
async def test_configured_app_does_not_spawn_initialization(monkeypatch):
    monkeypatch.setattr(configuration, "app_configured", lambda: "configured-app")
    async def forbidden():
        raise AssertionError("already configured app spawned initialization")
    monkeypatch.setattr(configuration, "app_init_flow", forbidden)
    assert (await LarkService().start_app_init())["app_id"] == "configured-app"
