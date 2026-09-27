import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.infra.ephemeral import LocalEphemeralState
from core.sandbox import evaluation_binding as binding
from core.sandbox.evaluation_provider import EvaluationSandboxProvider
from core.sandbox.errors import SandboxError
from core.sandbox.protocol import ProcessRequest


@pytest.fixture
def setup(monkeypatch):
    from core.sandbox import evaluation_process_guard
    monkeypatch.setattr(evaluation_process_guard, "clear_processes", AsyncMock())
    state = LocalEphemeralState()
    monkeypatch.setattr(binding, "get_ephemeral_state", lambda: state)
    commands = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(id="command")),
        get_command_status=AsyncMock(return_value=SimpleNamespace(running=False, exit_code=0, error=None)),
        get_background_command_logs=AsyncMock(return_value=SimpleNamespace(content="ok", cursor=2)),
        interrupt=AsyncMock(),
    )
    sandbox = SimpleNamespace(commands=commands, close=AsyncMock(),
                              files=SimpleNamespace(read_bytes=AsyncMock(return_value=b"file"), write_files=AsyncMock()))
    connect = AsyncMock(return_value=sandbox)
    return EvaluationSandboxProvider(connector=connect), sandbox, connect


async def test_connect_only_same_box_no_secret_environment(setup):
    provider, sandbox, connect = setup
    item = await binding.create("alice", "exact-box", 60)
    result = await provider.start_process(ProcessRequest(
        script_content="echo ok", script_name="a.sh", language="bash",
        session_id=item.session_id, user_id="alice"), yield_time_ms=0)
    assert result["exit_code"] == 0
    connect.assert_awaited_once_with("exact-box")
    opts = sandbox.commands.run.call_args.kwargs["opts"]
    assert not opts.envs
    assert await provider.get_file(item.session_id, "/app/a", user_id="alice") == b"file"
    connect.assert_awaited_once()


async def test_attach_failure_does_not_create_or_replace_binding(setup):
    provider, _, connect = setup
    connect.side_effect = RuntimeError("credential must not appear")
    item = await binding.create("alice", "missing-box", 60)
    with pytest.raises(SandboxError) as exc:
        await provider.get_file(item.session_id, "/a", user_id="alice")
    assert "credential" not in str(exc.value)
    assert (await binding.get(item.session_id)).sandbox_id == "missing-box"


async def test_frozen_rejects_all_agent_file_and_command_operations(setup):
    provider, _, _ = setup
    item = await binding.create("alice", "box", 60)
    await provider.freeze(item.session_id, "alice")
    with pytest.raises(SandboxError):
        await provider.get_file(item.session_id, "/a", user_id="alice")
    with pytest.raises(SandboxError):
        await provider.put_file(item.session_id, "/a", b"x", user_id="alice")
    with pytest.raises(SandboxError):
        await provider.write_stdin("handle", sandbox_session_id=item.session_id, user_id="alice")


async def test_freeze_interrupts_command_registered_by_another_worker(setup):
    provider, sandbox, connect = setup
    sandbox.commands.get_command_status.side_effect = [
        SimpleNamespace(running=True, exit_code=None, error=None),
        SimpleNamespace(running=True, exit_code=None, error=None),
        SimpleNamespace(running=False, exit_code=137, error=None),
    ]
    item = await binding.create("alice", "box", 60)
    await provider.start_process(ProcessRequest(script_content="sleep 100", script_name="a.sh",
        language="bash", session_id=item.session_id, user_id="alice"), yield_time_ms=0)
    other = EvaluationSandboxProvider(connector=connect)
    await other.freeze(item.session_id, "alice")
    sandbox.commands.interrupt.assert_awaited_once_with("command")
    assert (await binding.get(item.session_id)).phase == "frozen"


async def test_uncertain_launch_cannot_be_graded_or_retried(setup):
    provider, sandbox, _ = setup
    sandbox.commands.run.side_effect = RuntimeError("provider-secret")
    item = await binding.create("alice", "box", 60)
    with pytest.raises(SandboxError) as error:
        await provider.start_process(ProcessRequest(script_content="true", script_name="a.sh",
            language="bash", session_id=item.session_id, user_id="alice"))
    assert "provider-secret" not in str(error.value)
    assert (await binding.get(item.session_id)).uncertain
    with pytest.raises(SandboxError, match="Uncertain"):
        await provider.freeze(item.session_id, "alice")


async def test_freeze_has_total_deadline_and_keeps_agents_blocked(setup):
    provider, sandbox, _ = setup
    item = await binding.create("alice", "box", 60)
    async with binding.operation(item.session_id, "alice"):
        await binding.record_command(item.session_id, "alice", "handle", {"id": "command"})

    async def stall(*args):
        await asyncio.sleep(10)
    sandbox.commands.get_command_status.side_effect = stall
    with pytest.raises(SandboxError, match="TimeoutError"):
        await provider.freeze(item.session_id, "alice", timeout=0.01)
    assert (await binding.get(item.session_id)).phase == "freezing"
    with pytest.raises(SandboxError):
        await provider.get_file(item.session_id, "/a", user_id="alice")


async def test_close_keeps_tombstone_and_closes_only_local_client(setup):
    provider, sandbox, _ = setup
    item = await binding.create("alice", "box", 60)
    await provider.get_file(item.session_id, "/a", user_id="alice")
    await provider.close_session(item.session_id)
    assert (await binding.get(item.session_id)).phase == "closed"
    sandbox.close.assert_awaited_once()
    assert not provider._connections


async def test_owner_mismatch_never_connects(setup):
    provider, _, connect = setup
    item = await binding.create("alice", "box", 60)
    with pytest.raises(SandboxError, match="owner"):
        await provider.get_file(item.session_id, "/a", user_id="bob")
    connect.assert_not_awaited()


async def test_stream_export_overflow_removes_partial_file(setup, tmp_path):
    provider, sandbox, _ = setup
    item = await binding.create("alice", "box", 60)
    closed = []
    async def chunks():
        try:
            yield b"1234"
            yield b"5678"
        finally:
            closed.append(True)
    sandbox.files.read_bytes_stream = AsyncMock(return_value=chunks())
    path = tmp_path / "artifact"
    with pytest.raises(SandboxError):
        await provider.get_file_to_path(item.session_id, "/a", path, max_bytes=5, user_id="alice")
    assert not path.exists()
    assert closed == [True]


async def test_file_operation_drains_before_frozen_state(setup):
    provider, sandbox, _ = setup
    item = await binding.create("alice", "box", 60)
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(*args):
        entered.set()
        await release.wait()
        return b"x"
    sandbox.files.read_bytes.side_effect = read
    reading = asyncio.create_task(provider.get_file(item.session_id, "/a", user_id="alice"))
    await entered.wait()
    freezing = asyncio.create_task(provider.freeze(item.session_id, "alice"))
    while (await binding.get(item.session_id)).phase != "freezing":
        await asyncio.sleep(0)
    assert not freezing.done()
    with pytest.raises(SandboxError):
        await provider.get_file(item.session_id, "/b", user_id="alice")
    release.set()
    assert await reading == b"x"
    assert (await freezing).phase == "frozen"


async def test_partial_status_does_not_report_success(setup):
    provider, sandbox, _ = setup
    sandbox.commands.get_command_status.return_value = SimpleNamespace(running=None, exit_code=None, error=None)
    item = await binding.create("alice", "box", 60)
    with pytest.raises(SandboxError, match="incomplete"):
        await provider.start_process(ProcessRequest(script_content="true", script_name="a.sh",
            language="bash", session_id=item.session_id, user_id="alice"), yield_time_ms=0)


async def test_process_sweep_precedes_frozen_and_failure_keeps_agents_blocked(setup, monkeypatch):
    from core.sandbox import evaluation_process_guard
    provider, sandbox, _ = setup
    item = await binding.create("alice", "box", 60, protected_processes={"1": "123"})
    async def fail(sbx, baseline):
        assert sbx is sandbox
        assert baseline == {"1": "123"}
        assert (await binding.get(item.session_id)).phase == "freezing"
        raise SandboxError("cleanup failed")
    monkeypatch.setattr(evaluation_process_guard, "clear_processes", fail)
    with pytest.raises(SandboxError, match="cleanup failed"):
        await provider.freeze(item.session_id, "alice")
    assert (await binding.get(item.session_id)).phase == "freezing"
    with pytest.raises(SandboxError):
        await provider.get_file(item.session_id, "/a", user_id="alice")


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_confirmed_finished_command_skips_freeze_poll_but_still_sweeps(setup, exit_code):
    from core.sandbox import evaluation_process_guard
    provider, sandbox, _ = setup
    sandbox.commands.get_command_status.return_value = SimpleNamespace(
        running=False, exit_code=exit_code, error=None,
    )
    item = await binding.create("alice", "box", 60, protected_processes={"1": "123"})
    await provider.start_process(ProcessRequest(script_content="true", script_name="a.sh",
        language="bash", session_id=item.session_id, user_id="alice"), yield_time_ms=0)
    record = await binding.get(item.session_id)
    assert all(command["finished"] is True for command in record.commands.values())
    sandbox.commands.get_command_status.reset_mock()
    await provider.freeze(item.session_id, "alice")
    sandbox.commands.get_command_status.assert_not_awaited()
    sandbox.commands.interrupt.assert_not_awaited()
    evaluation_process_guard.clear_processes.assert_awaited_once_with(sandbox, {"1": "123"})


@pytest.mark.parametrize("record", [{}, {"finished": False}, {"finished": "true"}])
async def test_old_active_or_non_boolean_marker_still_checks_and_interrupts(setup, record):
    provider, sandbox, _ = setup
    item = await binding.create("alice", "box", 60)
    async with binding.operation(item.session_id, "alice"):
        await binding.record_command(item.session_id, "alice", "handle", {"id": "command", **record})
    sandbox.commands.get_command_status.side_effect = [
        SimpleNamespace(running=True, exit_code=None, error=None),
        SimpleNamespace(running=False, exit_code=137, error=None),
    ]
    await provider.freeze(item.session_id, "alice")
    assert sandbox.commands.get_command_status.await_count == 2
    sandbox.commands.interrupt.assert_awaited_once_with("command")


async def test_incomplete_status_is_never_cached_as_finished(setup):
    provider, sandbox, _ = setup
    sandbox.commands.get_command_status.return_value = SimpleNamespace(
        running=False, exit_code=None, error=None,
    )
    item = await binding.create("alice", "box", 60)
    with pytest.raises(SandboxError, match="incomplete"):
        await provider.start_process(ProcessRequest(script_content="true", script_name="a.sh",
            language="bash", session_id=item.session_id, user_id="alice"), yield_time_ms=0)
    record = await binding.get(item.session_id)
    assert all(command.get("finished") is not True for command in record.commands.values())
