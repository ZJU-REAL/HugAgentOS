"""Local/desktop sidecar startup contracts."""

import io
import os
import tarfile
from types import SimpleNamespace

import pytest
from mcp_servers import _serve
from orchestration import local_subprocess


def test_child_env_binds_local_mcp_to_loopback(monkeypatch):
    monkeypatch.setenv("MCP_HOST", "mcp")
    monkeypatch.delenv("MCP_BIND_HOST", raising=False)
    inherited = os.pathsep.join(["/existing/one", "/existing/two"])
    monkeypatch.setenv("PYTHONPATH", inherited)

    env = local_subprocess._child_env()

    assert env["MCP_HOST"] == "127.0.0.1"
    assert env["MCP_BIND_HOST"] == "127.0.0.1"
    pythonpath = env["PYTHONPATH"].split(os.pathsep)
    assert pythonpath[0] == local_subprocess._BACKEND_DIR
    assert pythonpath[1:] == inherited.split(os.pathsep)


def test_site_publish_callback_uses_local_listener_port(monkeypatch):
    from mcp_servers.site_publish_mcp import impl

    monkeypatch.delenv("BACKEND_INTERNAL_URL", raising=False)
    monkeypatch.delenv("BACKEND_PORT", raising=False)
    monkeypatch.setenv("PORT", "32101")

    assert impl._backend_url() == "http://127.0.0.1:32101"


@pytest.mark.asyncio
async def test_local_site_pack_uses_macos_portable_size_probe(monkeypatch):
    import core.sandbox as sandbox
    from core.llm.tools import _common
    from core.services import site_packaging

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

    files, error = await site_packaging.pack_and_fetch_dir(
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
            sandbox=SimpleNamespace(
                provider="script_runner",
                runner_url="http://127.0.0.1:32202",
            ),
        ),
    )
    monkeypatch.setattr(local_subprocess, "_spawn", fake_spawn)
    monkeypatch.setattr(local_subprocess, "_wait_for_mcp_ports", fake_wait)
    monkeypatch.setattr(local_subprocess, "_verify_required_plugin_tools", fake_verify)

    # 品牌隔离端口预检返回未占用；runner 就绪等待随后返回已就绪
    port_probes = iter([False, True, True, True])

    async def fake_port_ready(host, port):
        return next(port_probes, True)

    monkeypatch.setattr(local_subprocess, "_tcp_port_ready", fake_port_ready)

    await local_subprocess.start_local_sidecars()

    assert [call[1] for call in calls if call[0] == "spawn"] == [
        "mcp_launcher",
        "script_runner",
    ]
    runner_argv = next(call[2] for call in calls if call[:2] == ("spawn", "script_runner"))
    assert runner_argv[runner_argv.index("--port") + 1] == "32202"
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


def test_mcp_port_namespace_offset_is_validated():
    from mcp_servers import _ports

    assert _ports._parse_port_offset("23200") == 23200
    assert _ports._parse_port_offset(None) == 0
    with pytest.raises(RuntimeError, match="必须是整数"):
        _ports._parse_port_offset("not-a-port")
    with pytest.raises(RuntimeError, match="超出有效端口范围"):
        _ports._parse_port_offset("60000")


def test_script_runner_port_requires_loopback_url(monkeypatch):
    monkeypatch.setattr(
        local_subprocess,
        "settings",
        SimpleNamespace(
            sandbox=SimpleNamespace(runner_url="http://127.0.0.1:32202")
        ),
    )
    assert local_subprocess._script_runner_port() == 32202

    for invalid_url in (
        "http://runner.example:32202",
        "https://127.0.0.1:32202",
        "http://[::1]:32202",
    ):
        local_subprocess.settings.sandbox.runner_url = invalid_url
        with pytest.raises(RuntimeError, match="loopback"):
            local_subprocess._script_runner_port()


# ── 遗留孤儿 sidecar 清理（TCC 归因断裂根因，见 local_subprocess 模块注释） ──


def test_packaged_install_root_detected(monkeypatch):
    monkeypatch.setattr(
        local_subprocess.sys,
        "executable",
        "/Users/u/Library/Application Support/cn.x.desktop/local-server/releases/abc/venv/bin/python",
    )
    assert (
        local_subprocess._packaged_install_root()
        == "/Users/u/Library/Application Support/cn.x.desktop/local-server"
    )


def test_dev_interpreter_skips_reaping(monkeypatch):
    monkeypatch.setattr(local_subprocess.sys, "executable", "/usr/local/bin/python3")
    assert local_subprocess._packaged_install_root() is None


def test_find_stale_sidecar_pids_matches_only_own_install():
    root = "/Users/u/AS/cn.x.desktop/local-server"
    ps = "\n".join(
        [
            f"  100 {root}/releases/a/venv/bin/python -m uvicorn services.script_runner_service.server:app --host 127.0.0.1 --port 8900",
            f"  101 {root}/releases/a/venv/bin/python -m mcp_servers._launcher",
            f"  102 {root}/releases/a/venv/bin/python -m mcp_servers.internet_search_mcp.server --port 9102",
            # 其它产品同名 sidecar：安装根不同，不能杀
            "  103 /Users/u/AS/com.other.desktop/local-server/releases/b/venv/bin/python -m mcp_servers._launcher",
            # 主服务进程不是 sidecar
            f"  104 {root}/releases/a/venv/bin/hugagent serve --port 32101",
            # 指纹撞车但没有本安装根路径
            "  105 /bin/bash -c 'echo services.script_runner_service.server:app'",
            "  garbage line",
        ]
    )
    pids = local_subprocess._find_stale_sidecar_pids(ps, root_marker=root, my_pid=999)
    assert pids == [100, 101, 102]


def test_find_stale_sidecar_pids_excludes_self():
    root = "/r/local-server"
    ps = f"  42 {root}/venv/bin/python -m mcp_servers._launcher"
    assert (
        local_subprocess._find_stale_sidecar_pids(ps, root_marker=root, my_pid=42) == []
    )


@pytest.mark.asyncio
async def test_start_fails_fast_when_runner_port_is_occupied(monkeypatch):
    """清理后品牌 runner 端口仍被占时，不把执行面接到失控进程上。"""

    class DummyProcess:
        returncode = None
        pid = 42

    async def fake_spawn(label, argv):
        return DummyProcess()

    async def fake_reap():
        return None

    async def fake_port_ready(host, port):
        return True  # 预检即发现 8900 有监听者

    async def fake_stop():
        return None

    monkeypatch.setattr(
        local_subprocess,
        "settings",
        SimpleNamespace(
            deploy=SimpleNamespace(is_local=True),
            sandbox=SimpleNamespace(
                provider="script_runner",
                runner_url="http://127.0.0.1:32202",
            ),
        ),
    )
    monkeypatch.setattr(local_subprocess, "_spawn", fake_spawn)
    monkeypatch.setattr(local_subprocess, "_reap_stale_sidecars", fake_reap)
    monkeypatch.setattr(local_subprocess, "_tcp_port_ready", fake_port_ready)
    monkeypatch.setattr(local_subprocess, "stop_local_sidecars", fake_stop)

    with pytest.raises(RuntimeError, match="32202"):
        await local_subprocess.start_local_sidecars()


def test_stale_sidecar_match_folds_case_for_windows_paths():
    root = r"C:\Users\Aaron\AppData\Local\com.hugagent.desktop\local-server"
    table = "\n".join(
        [
            r"1200 c:\users\aaron\appdata\local\com.hugagent.desktop\local-server\r\p\x\python\python.exe -m uvicorn services.script_runner_service.server:app --port 32202",
            r"1201 C:\Users\Aaron\AppData\Local\com.hugagent.desktop\local-server\r\p\x\python\python.exe -m mcp_servers._launcher",
            r"1202 C:\Users\Aaron\AppData\Local\cn.hugagent.agent.desktop\local-server\r\p\x\python\python.exe -m uvicorn services.script_runner_service.server:app --port 8900",
            r"1203 C:\Users\Aaron\AppData\Local\com.hugagent.desktop\local-server\r\p\x\python\python.exe cli.py serve --port 32201",
        ]
    )
    assert local_subprocess._find_stale_sidecar_pids(table, root_marker=root, my_pid=1203) == [1201]
    assert local_subprocess._find_stale_sidecar_pids(
        table, root_marker=root, my_pid=1203, fold_case=True
    ) == [1200, 1201]


def test_listener_pid_from_netstat_picks_the_listening_socket():
    output = "\n".join(
        [
            "  Proto  Local Address          Foreign Address        State           PID",
            "  TCP    127.0.0.1:53022        127.0.0.1:32201        TIME_WAIT       0",
            "  TCP    127.0.0.1:32202        0.0.0.0:0              LISTENING       30400",
            "  TCP    0.0.0.0:322020         0.0.0.0:0              LISTENING       7",
        ]
    )
    assert local_subprocess._listener_pid_from_netstat(output, 32202) == 30400
    assert local_subprocess._listener_pid_from_netstat(output, 32201) is None


@pytest.mark.asyncio
async def test_windows_reap_terminates_stale_trees_before_starting(monkeypatch):
    """Windows 不再跳过启动期清理：按安装根找到孤儿后整树结束，再等端口释放。"""
    killed = []
    tables = iter(
        [
            r"77 C:\app\local-server\r\p\x\python\python.exe -m uvicorn services.script_runner_service.server:app --port 32202",
            "",
        ]
    )

    async def fake_table():
        return next(tables)

    async def fake_kill(pids):
        killed.extend(pids)

    port_probe = iter([True, False])

    async def fake_port_ready(host, port):
        return next(port_probe)

    monkeypatch.setattr(local_subprocess.os, "name", "nt")
    monkeypatch.setattr(local_subprocess, "_packaged_install_root", lambda: r"C:\app\local-server")
    monkeypatch.setattr(local_subprocess, "_process_table", fake_table)
    monkeypatch.setattr(local_subprocess, "_terminate_process_trees", fake_kill)
    monkeypatch.setattr(local_subprocess, "_tcp_port_ready", fake_port_ready)
    monkeypatch.setattr(
        local_subprocess,
        "settings",
        SimpleNamespace(sandbox=SimpleNamespace(provider="script_runner", runner_url="http://127.0.0.1:32202")),
    )
    await local_subprocess._reap_stale_sidecars()
    assert killed == [77]


@pytest.mark.asyncio
async def test_windows_stop_terminates_whole_sidecar_tree(monkeypatch):
    killed = []

    async def fake_kill(pids):
        killed.extend(pids)

    class DummyProcess:
        pid = 4242
        returncode = None

        async def wait(self):
            self.returncode = 0

    monkeypatch.setattr(local_subprocess.os, "name", "nt")
    monkeypatch.setattr(local_subprocess, "_terminate_process_trees", fake_kill)
    monkeypatch.setattr(local_subprocess, "_PROCS", [("script_runner", DummyProcess())])
    await local_subprocess.stop_local_sidecars()
    assert killed == [4242]


@pytest.mark.asyncio
async def test_cloud_tools_mode_starts_only_the_script_runner(monkeypatch):
    """混合模式：工具来自云端，本机只起脚本执行 sidecar，不碰 MCP launcher。"""
    calls = []

    class DummyProcess:
        pid = 31
        returncode = None

    async def fake_spawn(label, argv):
        calls.append(("spawn", label, argv))
        return DummyProcess()

    async def fake_reap():
        calls.append(("reap",))

    async def fail_wait(*_a, **_k):
        raise AssertionError("MCP ports must not be awaited in cloud-tools mode")

    port_probes = iter([False, True])

    async def fake_port_ready(host, port):
        return next(port_probes)

    monkeypatch.setattr(local_subprocess, "_cloud_serves_tools", lambda: True)
    monkeypatch.setattr(local_subprocess, "_reap_stale_sidecars", fake_reap)
    monkeypatch.setattr(local_subprocess, "_spawn", fake_spawn)
    monkeypatch.setattr(local_subprocess, "_wait_for_mcp_ports", fail_wait)
    monkeypatch.setattr(local_subprocess, "_verify_required_plugin_tools", fail_wait)
    monkeypatch.setattr(local_subprocess, "_tcp_port_ready", fake_port_ready)
    monkeypatch.setattr(local_subprocess, "_start_watchdog", lambda: calls.append(("watchdog",)))
    monkeypatch.setattr(local_subprocess, "_PROCS", [])
    monkeypatch.setattr(
        local_subprocess,
        "settings",
        SimpleNamespace(
            deploy=SimpleNamespace(is_local=True),
            sandbox=SimpleNamespace(provider="script_runner", runner_url="http://127.0.0.1:32202"),
        ),
    )
    await local_subprocess.start_local_sidecars()
    assert [c[1] for c in calls if c[0] == "spawn"] == ["script_runner"]
    assert ("watchdog",) in calls

