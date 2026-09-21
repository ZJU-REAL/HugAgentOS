"""Path identities must agree with the selected execution environment."""

from dataclasses import replace
import importlib
from pathlib import Path
import pytest
from core.llm.tools import _paths
from core.config import local_mode
from core.config.settings import settings


@pytest.mark.parametrize("provider", ["cube", "opensandbox"])
def test_container_relative_files_use_execution_cwd(monkeypatch, provider):
    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: False)
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", "/workspace")
    monkeypatch.setattr(
        importlib.import_module("core.config.settings"),
        "settings",
        replace(settings, sandbox=replace(settings.sandbox, provider=provider)),
    )
    assert _paths.to_physical_path("foo.txt", "owner", session_id="chat") == "/workspace/foo.txt"


def test_desktop_uses_session_and_keeps_absolute_paths(monkeypatch, tmp_path):
    from services.script_runner_service.workspace_paths import session_root

    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path))
    assert _paths.to_physical_path("foo.txt", "owner", session_id="chat") == str(
        Path(session_root(str(tmp_path), "chat")) / "foo.txt"
    )
    assert (
        _paths.to_physical_path("/workspace/foo.txt", "owner", session_id="chat")
        == "/workspace/foo.txt"
    )


@pytest.mark.asyncio
async def test_skill_runtime_hint_runs_in_current_directory_with_spaces(monkeypatch, tmp_path):
    import json
    import subprocess
    import os
    import sys
    from types import SimpleNamespace
    from core.llm.tools.skill_tool import register_sandboxed_view_text_file

    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    skill = tmp_path / "技能 Alice's tools"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: test\n---\nUse {baseDir}.")
    (skill / "run.py").write_text("from pathlib import Path; Path('output.txt').write_text('ok')")

    class Toolkit:
        def register_tool_function(self, fn, **kwargs):
            self.fn = fn

    loader = SimpleNamespace(
        load_skill_full=lambda _: SimpleNamespace(
            name="example", executable_scripts=[{"name": "run.py"}]
        ),
        get_skill_source=lambda _: "local",
    )
    tk = Toolkit()
    register_sandboxed_view_text_file(tk, [str(skill)], loader)
    response = await tk.fn(str(skill / "SKILL.md"))
    text = "\n".join(
        block.get("text", "") if isinstance(block, dict) else block.text
        for block in response.content
    )
    hint = next(
        line.strip() for line in text.splitlines() if line.strip().startswith("bash(command=")
    )
    command = json.loads(hint[len("bash(command=") : -1])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(
        "PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    result = subprocess.run(["bash", "-c", command], cwd=workspace, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (workspace / "output.txt").read_text() == "ok"
    assert not (skill / "output.txt").exists()
    denied = await tk.fn("/workspace/skills/example/SKILL.md")
    assert "Access denied" in str(denied.content)


def test_windows_shell_path_preserves_unicode_spaces_and_drive():
    import shlex
    from core.sandbox.desktop_paths import quote_shell_path

    value = r"C:\Users\张 三\工具包\run.py"
    assert shlex.split(quote_shell_path(value)) == ["C:/Users/张 三/工具包/run.py"]


@pytest.mark.asyncio
async def test_desktop_relative_artifact_roundtrip(monkeypatch, tmp_path):
    import base64
    import json
    from core.llm.tools import sandbox_tool
    from services.script_runner_service import server

    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))

    class Provider:
        async def put_file(self, session_id, path, content, user_id=None):
            await server.put_file(
                server.PutFileRequest(
                    session_id=session_id,
                    path=path,
                    user_id=user_id,
                    content_b64=base64.b64encode(content).decode(),
                )
            )

        async def get_file_to_path(self, session_id, path, destination, **kwargs):
            result = await server.get_file(server.GetFileRequest(session_id=session_id, path=path))
            data = base64.b64decode(result.content_b64)
            destination.write_bytes(data)
            return len(data)

    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: Provider())
    monkeypatch.setattr(
        sandbox_tool,
        "_resolve_artifact_files",
        lambda files, user: (
            {path: base64.b64encode(b"input-data").decode() for path in files},
            None,
        ),
    )
    captured = []

    def store(path, **kwargs):
        captured.append(path.read_bytes())
        return dict(
            file_id="result",
            name="output.txt",
            url="/files/result",
            mime_type="text/plain",
            size=10,
        )

    monkeypatch.setattr(sandbox_tool, "_store_generated_file_path", store)

    class Toolkit:
        def register_tool_function(self, fn, **kwargs):
            self.fn = fn

    put, get = Toolkit(), Toolkit()
    sandbox_tool.register_sandbox_put_artifact(put, chat_id="chat", user_id="owner")
    sandbox_tool.register_sandbox_get_artifact(get, chat_id="chat", user_id="owner")

    def payload(result):
        return json.loads(result.content[0].text)

    assert payload(await put.fn("upload", "input.txt"))["ok"]
    result = await server.execute(
        server.ExecuteRequest(
            session_id="chat",
            language="python",
            script_name="copy.py",
            script_content="from pathlib import Path; Path('output.txt').write_bytes(Path('input.txt').read_bytes())",
        )
    )
    assert result.exit_code == 0, result.stderr
    assert payload(await get.fn("output.txt"))["ok"]
    assert captured == [b"input-data"]
    assert "error" in payload(await get.fn("../outside.txt"))


def test_desktop_runner_creates_no_aliases(monkeypatch, tmp_path):
    from services.script_runner_service import server

    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "skills").mkdir()
    workspace = server._session_workspace("chat", create=True, user_id="owner")
    assert list(workspace.iterdir()) == []
    assert not (tmp_path / "myspace").exists()


@pytest.mark.asyncio
async def test_job_commands_preserve_paths_with_spaces(monkeypatch, tmp_path):
    import asyncio
    from orchestration import job_runtime

    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    root = tmp_path / "Application Support" / "张 三"
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(root))

    async def callback(**kwargs):
        return "http://127.0.0.1/unused"

    async def bash(command, **kwargs):
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    monkeypatch.setattr(job_runtime, "resolve_callback_base", callback)
    monkeypatch.setattr(job_runtime, "_sbx_bash", bash)
    await job_runtime.prepare_and_launch(
        "job-1",
        user_id="owner",
        session_id="chat",
        script_text="pass",
        token="test",
        interpreter="true",
    )
    workspace = Path(_paths.workspace_directory("chat"))
    assert (workspace / ".job/job-1/user_script.py").read_text() == "pass"
    output = workspace / "导出 数据.txt"
    ok, detail = await job_runtime.write_sandbox_file(
        str(output),
        "中文 data",
        session_id="chat",
        user_id="owner",
    )
    assert ok, detail
    assert output.read_text() == "中文 data"
