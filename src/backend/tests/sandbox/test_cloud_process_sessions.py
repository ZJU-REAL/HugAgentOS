"""Cloud SDK contract tests for managed commands (no production connection)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from core.sandbox.protocol import ProcessRequest


async def test_opensandbox_starts_without_execution_timeout_and_interrupts_owner_only():
    from core.sandbox.cloud_processes import CloudProcesses

    commands = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(id="remote-1")),
        get_command_status=AsyncMock(
            return_value=SimpleNamespace(running=True, exit_code=None, error=None)
        ),
        get_background_command_logs=AsyncMock(
            return_value=SimpleNamespace(content="hello", cursor=1)
        ),
        interrupt=AsyncMock(),
    )
    provider = SimpleNamespace(
        name="opensandbox",
        _get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(sandbox=SimpleNamespace(commands=commands))
        ),
        _sync_inputs=AsyncMock(),
        touch_session=AsyncMock(return_value=True),
    )
    service = CloudProcesses(provider)
    try:
        first = await service.start(
            ProcessRequest(
                script_content="sleep 999",
                script_name="x.sh",
                language="bash",
                session_id="a",
                user_id="alice",
                timeout=None,
            ),
            250,
        )
        assert first["status"] == "running"
        opts = commands.run.call_args.kwargs["opts"]
        assert opts.background is True
        assert opts.timeout is None
        from core.sandbox.errors import SandboxError

        with pytest.raises(SandboxError, match="Unknown process session"):
            await service.write(
                first["session_id"],
                sandbox_session_id="b",
                user_id="alice",
                chars="\x03",
                yield_time_ms=0,
            )
        commands.interrupt.assert_not_called()
        await service.write(
            first["session_id"],
            sandbox_session_id="a",
            user_id="alice",
            chars="\x03",
            yield_time_ms=0,
        )
        commands.interrupt.assert_awaited_once_with("remote-1")
    finally:
        await service.close_all()


async def test_opensandbox_final_logs_and_nonzero_exit_are_not_lost():
    from core.sandbox.cloud_processes import CloudProcesses

    commands = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(id="remote-1")),
        get_command_status=AsyncMock(
            return_value=SimpleNamespace(running=False, exit_code=7, error=None)
        ),
        get_background_command_logs=AsyncMock(
            return_value=SimpleNamespace(content="final", cursor=1)
        ),
        interrupt=AsyncMock(),
    )
    provider = SimpleNamespace(
        name="opensandbox",
        _get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(sandbox=SimpleNamespace(commands=commands))
        ),
        _sync_inputs=AsyncMock(),
        touch_session=AsyncMock(return_value=True),
    )
    service = CloudProcesses(provider)
    try:
        result = await service.start(
            ProcessRequest(
                script_content="exit 7",
                script_name="x.sh",
                language="bash",
                session_id="a",
                timeout=None,
            ),
            250,
        )
        assert result["stdout"] == "final"
        assert result["exit_code"] == 7
        commands.interrupt.assert_not_called()
        provider.touch_session.assert_awaited_with("a")
    finally:
        await service.close_all()


async def test_cube_uses_background_without_connection_deadline_and_bounds_lifetime():
    import asyncio
    from core.sandbox.cloud_processes import CloudProcesses

    command = SimpleNamespace(
        wait=AsyncMock(return_value=SimpleNamespace(exit_code=0)), kill=AsyncMock()
    )

    async def run(cmd, **kwargs):
        assert kwargs["background"] is True
        assert kwargs["timeout"] == 0
        assert kwargs["stdin"] is False
        kwargs["on_stdout"]("cube-output")
        return command

    sandbox = SimpleNamespace(commands=SimpleNamespace(run=AsyncMock(side_effect=run)))
    provider = SimpleNamespace(
        name="cube",
        _request_timeout_s=30,
        _get_session_lock=AsyncMock(return_value=asyncio.Lock()),
        _acquire_persistent=AsyncMock(return_value=sandbox),
        _prepare_command=AsyncMock(),
        _myspace_prefix=lambda uid: "",
        touch_session=AsyncMock(return_value=True),
    )
    service = CloudProcesses(provider)
    try:
        result = await service.start(
            ProcessRequest(
                script_content="echo cube-output",
                script_name="x.sh",
                language="bash",
                session_id="a",
                timeout=None,
            ),
            1000,
        )
        assert result["exit_code"] == 0
        assert result["stdout"] == "cube-output"
        assert "cannot renew" in result["lifetime_note"]
    finally:
        await service.close_all()


async def test_runner_provider_checks_capability_integrity_before_returning_output(monkeypatch):
    import httpx
    from core.capabilities.errors import IntegrityFailed
    from core.sandbox.script_runner_provider import ScriptRunnerProvider

    requests = []

    async def request(req):
        import json

        requests.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "status": "running",
                "session_id": "p1",
                "stdout": "untrusted",
                "stderr": "",
                "exit_code": None,
                "_capability": {"run_id": "run-1", "scope": "scope-a", "user_id": "alice"},
            },
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(request), **kwargs),
    )
    monkeypatch.setattr("core.capabilities.runtime.get", lambda *args, **kwargs: object())

    def invalid(*args, **kwargs):
        raise IntegrityFailed("changed capability")

    monkeypatch.setattr("core.capabilities.runtime.validate", invalid)
    with pytest.raises(IntegrityFailed, match="changed capability"):
        await ScriptRunnerProvider().write_stdin(
            "p1", sandbox_session_id="chat-a", user_id="alice", yield_time_ms=0
        )
    assert len(requests) == 2
    assert requests[1]["chars"] == "\x03"
    assert requests[1]["sandbox_session_id"] == "chat-a"


async def test_runner_start_does_not_retry_an_ambiguous_network_failure(monkeypatch):
    import httpx
    from core.sandbox.script_runner_provider import ScriptRunnerProvider
    from core.sandbox.errors import SandboxError

    calls = []

    async def request(req):
        calls.append(req)
        raise httpx.ReadTimeout("response lost", request=req)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(request), **kwargs),
    )
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider._refresh_skill_view", lambda *args: None
    )
    with pytest.raises(SandboxError, match="uncertain"):
        await ScriptRunnerProvider().start_process(
            ProcessRequest(
                script_content="long command", script_name="x.sh", session_id="a", timeout=None
            )
        )
    assert len(calls) == 1


async def test_completion_client_waits_same_process_and_joins_increments():
    from core.sandbox.process_completion import CompletionMixin

    provider = SimpleNamespace(
        start_process=AsyncMock(
            return_value=dict(
                status="running", session_id="p1", stdout="first", stderr="", exit_code=None
            )
        ),
        write_stdin=AsyncMock(
            return_value=dict(
                status="exited",
                session_id=None,
                stdout="second",
                stderr="warning",
                exit_code=7,
                execution_time_ms=123,
            )
        ),
    )
    req = ProcessRequest("sleep 1", "task.sh", language="bash", session_id="chat", user_id="alice")
    result = await CompletionMixin.run_to_completion(provider, req)
    assert (result.stdout, result.stderr, result.exit_code) == ("firstsecond", "warning", 7)
    assert provider.start_process.await_count == 1
    provider.write_stdin.assert_awaited_once_with(
        "p1", sandbox_session_id="chat", user_id="alice", yield_time_ms=60000
    )
    assert req.timeout is None


async def test_completion_client_fails_on_omitted_output_instead_of_parsing_a_partial_result():
    from core.sandbox.process_completion import CompletionMixin
    from core.sandbox import SandboxError

    provider = SimpleNamespace(
        start_process=AsyncMock(
            return_value=dict(
                status="exited",
                exit_code=0,
                stdout="partial",
                output_omitted_chars=42,
            )
        )
    )
    with pytest.raises(SandboxError, match="omitted"):
        await CompletionMixin.run_to_completion(
            provider, ProcessRequest("probe", "probe.sh", session_id="chat")
        )


@pytest.mark.parametrize("language", ["python", "javascript"])
async def test_cloud_script_resources_arguments_and_json_stdin(tmp_path, monkeypatch, language):
    import asyncio
    from core.sandbox.cloud_processes import CloudProcesses

    captured = {}
    (tmp_path / "helper.py").write_text("VALUE=42")
    (tmp_path / "helper.js").write_text("exports.VALUE=42")
    (tmp_path / "sibling.txt").write_text("sibling")
    script = (
        "import helper,json,sys; from pathlib import Path; print(sys.argv[1]); print(json.load(sys.stdin)['value']); print(helper.VALUE); print(Path(__file__).with_name('sibling.txt').read_text())"
        if language == "python"
        else "const fs=require('fs'); console.log(process.argv[2]); console.log(JSON.parse(fs.readFileSync(0,'utf8')).value); console.log(require('./helper').VALUE); console.log(fs.readFileSync(require('path').join(__dirname,'sibling.txt'),'utf8'));"
    )
    monkeypatch.setattr("core.sandbox._common.WORKSPACE", str(tmp_path))

    async def run(command, opts):
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=opts.working_directory,
        )
        out, err = await proc.communicate()
        captured.update(out=out.decode(), err=err.decode(), code=proc.returncode)
        return SimpleNamespace(id="python-process")

    commands = SimpleNamespace(
        run=run,
        get_command_status=AsyncMock(
            side_effect=lambda _: SimpleNamespace(
                running=False, exit_code=captured["code"], error=None
            )
        ),
        get_background_command_logs=AsyncMock(
            side_effect=lambda *a, **kw: SimpleNamespace(content=captured["out"], cursor=1)
        ),
        interrupt=AsyncMock(),
    )
    provider = SimpleNamespace(
        name="opensandbox",
        _get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(sandbox=SimpleNamespace(commands=commands))
        ),
        _sync_inputs=AsyncMock(),
        touch_session=AsyncMock(),
    )
    service = CloudProcesses(provider)
    try:
        result = await service.start(
            ProcessRequest(
                script,
                "input",
                language=language,
                params={"_args": ["--two words"], "value": "quote ' dollar $"},
                session_id="cloud-input",
            ),
            1000,
        )
        assert result["exit_code"] == 0, captured
        assert result["stdout"].splitlines() == ["--two words", "quote ' dollar $", "42", "sibling"]
        assert not list(tmp_path.glob(".__process_script_*"))
    finally:
        await service.close_all()


async def test_completion_cancellation_during_start_cleans_launched_process():
    import asyncio
    from core.sandbox.process_completion import CompletionMixin

    started, release = asyncio.Event(), asyncio.Event()

    async def start(req, yield_time_ms):
        started.set()
        await release.wait()
        return dict(status="running", session_id="p1", stdout="", stderr="", exit_code=None)

    provider = SimpleNamespace(
        start_process=start,
        write_stdin=AsyncMock(return_value={"status": "exited", "exit_code": 130}),
    )
    task = asyncio.create_task(
        CompletionMixin.run_to_completion(
            provider, ProcessRequest("sleep 99", "task.sh", session_id="chat")
        )
    )
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.write_stdin.await_count == 1
    assert provider.write_stdin.call_args.kwargs["chars"] == chr(3)


async def test_internal_provider_completion_returns_final_result_without_harness(monkeypatch):
    import json
    import httpx
    from core.sandbox.script_runner_provider import ScriptRunnerProvider

    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/processes/start":
            assert body["timeout"] is None
            return httpx.Response(
                200,
                json=dict(
                    status="running",
                    session_id="process-1",
                    stdout="first",
                    stderr="",
                    exit_code=None,
                ),
            )
        assert request.url.path == "/processes/write"
        assert body["session_id"] == "process-1"
        assert body["sandbox_session_id"] == "internal-job"
        return httpx.Response(
            200,
            json=dict(
                status="exited",
                session_id=None,
                stdout="second",
                stderr="",
                exit_code=3,
                execution_time_ms=150000,
            ),
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr("core.sandbox.script_runner_provider._refresh_skill_view", lambda *a: None)
    result = await ScriptRunnerProvider().run_to_completion(
        ProcessRequest("sleep 150", "job.sh", language="bash", session_id="internal-job")
    )
    assert result.stdout == "firstsecond"
    assert result.exit_code == 3
    assert result.execution_time_ms == 150000
    assert [path for path, _ in requests] == ["/processes/start", "/processes/write"]
