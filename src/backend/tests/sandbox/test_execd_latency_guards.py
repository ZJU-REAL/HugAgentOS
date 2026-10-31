"""execd v1.0.17 workarounds: no post-completion stream delay, and the launch race."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.sandbox.processes.cloud import OpenSandboxHandle
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider


class _Commands:
    def __init__(self, misses):
        self.misses = misses

    async def get_command_status(self, _id):
        if self.misses:
            self.misses -= 1
            raise RuntimeError("Get command status failed: command not found: abc")
        return SimpleNamespace(running=False, exit_code=0, error=None)

    async def get_background_command_logs(self, _id, cursor=0):
        return SimpleNamespace(cursor=3, content="ok\n")


def _handle(commands, launched_at):
    handle = OpenSandboxHandle(
        SimpleNamespace(name="opensandbox"), SimpleNamespace(session_id="s"),
        commands, "abc", launched_at=launched_at,
    )
    handle.touch = AsyncMock()
    return handle


async def test_status_miss_right_after_launch_reads_as_still_starting():
    handle = _handle(_Commands(misses=1), launched_at=time.monotonic())
    assert await handle.poll() == ("", "", None)
    assert await handle.poll() == ("ok\n", "", 0)


async def test_status_miss_after_the_window_is_an_error():
    handle = _handle(_Commands(misses=1), launched_at=time.monotonic() - 10)
    with pytest.raises(RuntimeError, match="command not found"):
        await handle.poll()


async def test_recovered_handle_gets_no_window():
    handle = _handle(_Commands(misses=1), launched_at=None)
    with pytest.raises(RuntimeError, match="command not found"):
        await handle.poll()


@pytest.mark.parametrize("user_id", [None, "u1"])
async def test_every_sandbox_starts_execd_without_the_stream_grace_period(user_id):
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    provider._make_config = lambda: None
    provider._session_expiry_s = lambda: 60
    provider._image = "image"
    provider._additional_workspace_mounts = AsyncMock(return_value=([], {}))
    provider._create_owned_sandbox = AsyncMock(return_value=SimpleNamespace(id="sbx"))
    import core.sandbox.providers.opensandbox.creation as creation

    original = creation._make_skills_volumes
    creation._make_skills_volumes = lambda _uid: []
    try:
        await provider._create_jupyter_sandbox(user_id=user_id)
    finally:
        creation._make_skills_volumes = original
    env = provider._create_owned_sandbox.await_args.kwargs["env"]
    assert env["EXECD_API_GRACE_SHUTDOWN"] == "0s"
    assert ("JX_MYSPACE_UID" in env) is bool(user_id)
