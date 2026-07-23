"""Local/desktop sidecar startup contracts."""

import io
import tarfile
from types import SimpleNamespace

import pytest
from mcp_servers import _serve
from orchestration import local_subprocess


def test_child_env_binds_local_mcp_to_loopback(monkeypatch):
    monkeypatch.setenv("MCP_HOST", "mcp")
    monkeypatch.delenv("MCP_BIND_HOST", raising=False)

    env = local_subprocess._child_env()

    assert env["MCP_HOST"] == "127.0.0.1"
    assert env["MCP_BIND_HOST"] == "127.0.0.1"


def test_site_publish_callback_uses_local_listener_port(monkeypatch):
    from mcp_servers.site_publish_mcp import impl

    monkeypatch.delenv("BACKEND_INTERNAL_URL", raising=False)
    monkeypatch.delenv("BACKEND_PORT", raising=False)
    monkeypatch.setenv("PORT", "32101")

    assert impl._backend_url() == "http://127.0.0.1:32101"


@pytest.mark.asyncio
async def test_local_site_pack_uses_macos_portable_size_probe(monkeypatch):
    import core.sandbox as sandbox
    from api.routes.v1 import internal_sites
    from core.llm.tools import _common

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tf:
        content = b"<h1>desktop site</h1>"
        info = tarfile.TarInfo("./index.html")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))

    commands = []

    async def fake_exec(command, *, chat_id, timeout=30):
        commands.append(command)
        if command.startswith("rm -f "):
            return 0, "", ""
        return 0, f"  {len(archive.getvalue())}\n", ""

    class FakeProvider:
        async def get_file(self, session_id, path, user_id=None):
            return archive.getvalue()

    monkeypatch.setattr(_common, "sandbox_exec_bash", fake_exec)
    monkeypatch.setattr(sandbox, "get_sandbox_provider", lambda: FakeProvider())

    files, error = await internal_sites._pack_and_fetch_dir(
        "/workspace/site with spaces", "chat-local", "user-local"
    )

    assert error is None
    assert files == [("index.html", b"<h1>desktop site</h1>")]
    assert "wc -c <" in commands[0]
    assert "du -b" not in commands[0]
    assert "'/workspace/site with spaces'" in commands[0]


def test_streamable_http_bind_host_defaults_to_compose_and_supports_local(monkeypatch):
    monkeypatch.delenv("MCP_BIND_HOST", raising=False)
    assert _serve._streamable_http_bind_host() == "0.0.0.0"

    monkeypatch.setenv("MCP_BIND_HOST", "127.0.0.1")
    assert _serve._streamable_http_bind_host() == "127.0.0.1"


def test_required_default_plugin_servers_are_launchable():
    from mcp_servers._launcher import PORTS as launcher_ports
    from mcp_servers._ports import PORTS, package_name

    for server_id, expected_tool in local_subprocess._REQUIRED_PLUGIN_MCP_TOOLS.items():
        assert expected_tool
        assert server_id in PORTS
        assert launcher_ports[package_name(server_id)] == PORTS[server_id]


@pytest.mark.asyncio
async def test_local_start_waits_for_ports_and_verifies_plugin_tools(monkeypatch):
    calls = []

    class DummyProcess:
        returncode = None
        pid = 42

    async def fake_spawn(label, argv):
        calls.append(("spawn", label, tuple(argv)))
        return DummyProcess()

    async def fake_wait(launcher, ports, *, timeout):
        calls.append(("wait", launcher.pid, dict(ports), timeout))

    async def fake_verify(ports):
        calls.append(("verify", dict(ports)))

    monkeypatch.setattr(
        local_subprocess,
        "settings",
        SimpleNamespace(
            deploy=SimpleNamespace(is_local=True),
            sandbox=SimpleNamespace(provider="script_runner"),
        ),
    )
    monkeypatch.setattr(local_subprocess, "_spawn", fake_spawn)
    monkeypatch.setattr(local_subprocess, "_wait_for_mcp_ports", fake_wait)
    monkeypatch.setattr(local_subprocess, "_verify_required_plugin_tools", fake_verify)

    await local_subprocess.start_local_sidecars()

    assert [call[1] for call in calls if call[0] == "spawn"] == [
        "mcp_launcher",
        "script_runner",
    ]
    waited_ports = next(call[2] for call in calls if call[0] == "wait")
    assert set(local_subprocess._REQUIRED_PLUGIN_MCP_TOOLS) <= set(waited_ports)
    assert any(call[0] == "verify" for call in calls)


@pytest.mark.asyncio
async def test_required_plugin_tool_contract_rejects_missing_registration():
    with pytest.raises(RuntimeError, match="site_publish"):
        await local_subprocess._verify_required_plugin_tools(
            {
                "automation_task": 9108,
                "skill_manager": 9112,
            }
        )
