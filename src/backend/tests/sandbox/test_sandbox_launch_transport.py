"""The sandbox launch survives the trip from the permission layer to the spawn.

The confinement is decided in the backend and enforced in the sidecar, which is
a process boundary and a JSON hop. Everything here guards that seam: the two
definitions of the launch agree, the provider actually sends it, and the runner
applies it to the argv and environment it spawns with — a break anywhere along
that path would leave a command running unconfined while every layer believed
otherwise.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import sys

import pytest
from core.sandbox.os_sandbox import LocalAccessDecision, build_context, build_policy, confine
from core.sandbox.oslayer import SandboxLaunch
from core.sandbox.protocol import ExecuteRequest
from services.script_runner_service import server


def test_both_ends_describe_the_same_launch():
    backend_fields = {field.name for field in dataclasses.fields(SandboxLaunch)}
    assert backend_fields == set(server.SandboxLaunch.model_fields)


def test_a_launch_round_trips_through_the_wire_model():
    launch = SandboxLaunch(
        backend="bwrap", argv_prefix=("bwrap", "--unshare-net", "--"), env={"TMPDIR": "/scratch"}
    )
    wire = server.SandboxLaunch(**launch.to_json())

    assert wire.backend == launch.backend
    assert tuple(wire.argv_prefix) == launch.argv_prefix
    assert wire.env == dict(launch.env)
    assert SandboxLaunch.from_json(wire.model_dump()) == launch


def test_the_provider_sends_the_launch_to_the_sidecar(monkeypatch):
    from core.sandbox.script_runner_provider import ScriptRunnerProvider

    captured: dict = {}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"stdout": "", "stderr": "", "exit_code": 0, "execution_time_ms": 1, "files": []}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["body"] = json
            return _Response()

    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient", lambda **_: _Client()
    )
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider._refresh_skill_view", lambda *_args, **_kw: None
    )

    launch = SandboxLaunch(backend="bwrap", argv_prefix=("bwrap", "--"), env={"A": "B"})
    asyncio.run(
        ScriptRunnerProvider().execute(
            ExecuteRequest(
                script_content="ls",
                script_name="_bash.sh",
                language="bash",
                session_id="chat-1",
                sandbox_launch=launch,
            )
        )
    )

    assert captured["body"]["sandbox_launch"] == launch.to_json()


def test_no_launch_is_sent_as_null():
    request = ExecuteRequest(
        script_content="ls", script_name="_bash.sh", language="bash", session_id="chat-1"
    )
    assert request.sandbox_launch is None


@pytest.mark.parametrize("launch", [None, server.SandboxLaunch(backend="test")])
def test_the_runner_spawns_with_the_prefix_and_env_it_was_given(monkeypatch, tmp_path, launch):
    captured: dict = {}

    class _Process:
        returncode = 0

        async def wait(self):
            return 0

    async def _fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs["env"]
        return _Process()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(server, "_wait_for_process_exit", lambda proc: _noop())
    monkeypatch.setattr(server, "_terminate_process_group", _noop)

    if launch is not None:
        launch = server.SandboxLaunch(
            backend="test", argv_prefix=["sbx", "--flag", "--"], env={"SBX": "1"}
        )

    asyncio.run(
        server._execute_subprocess(
            cmd=["bash", "script.sh"],
            stdin_data="{}",
            timeout=5,
            cwd=str(tmp_path),
            sandbox_launch=launch,
        )
    )

    if launch is None:
        assert captured["cmd"] == ["bash", "script.sh"]
        assert "SBX" not in captured["env"]
    else:
        assert captured["cmd"] == ["sbx", "--flag", "--", "bash", "script.sh"]
        assert captured["env"]["SBX"] == "1"
        # The overlay adds to the runner's safe environment rather than replacing it.
        assert set(server.SAFE_ENV).issubset(captured["env"])


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="真机验证需要 Linux 与 bubblewrap",
)
def test_the_runner_really_confines_a_command_end_to_end(monkeypatch, tmp_path):
    """The whole seam, for real: policy → launch → sidecar spawn → kernel.

    Everything else here checks that the pieces line up. This one runs an actual
    command through the actual runner and confirms the kernel refused a write
    the policy did not grant — the only assertion that would still catch a
    sandbox that lines up perfectly and confines nothing.

    Runs under the local profile because that is the only profile that produces
    a launch at all, and the difference matters here: the container profile caps
    ``RLIMIT_NPROC`` for the whole login user, which bubblewrap hits while
    creating its user namespace.
    """
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    launch = confine(
        build_policy(LocalAccessDecision(approval_mode="ask", writable_roots=(str(workspace),))),
        build_context(workspace_root=str(workspace)),
    )
    wire = server.SandboxLaunch(**launch.to_json())

    def _run(script: str) -> dict:
        # Inside the workspace, exactly where the sidecar puts the script it is
        # about to run: scratch space is private to the sandbox, so a script
        # staged in the host's temp directory would not be visible from inside.
        script_path = workspace / "probe.sh"
        script_path.write_text(script, encoding="utf-8")
        return asyncio.run(
            server._execute_subprocess(
                cmd=["bash", str(script_path)],
                stdin_data="{}",
                timeout=30,
                cwd=str(workspace),
                sandbox_launch=wire,
            )
        )

    inside = _run(f"echo ok > {workspace}/written.txt")
    assert inside["exit_code"] == 0, inside
    assert (workspace / "written.txt").read_text(encoding="utf-8").strip() == "ok"

    blocked = _run(f"echo nope > {outside}/written.txt")
    assert blocked["exit_code"] != 0
    assert not (outside / "written.txt").exists()
