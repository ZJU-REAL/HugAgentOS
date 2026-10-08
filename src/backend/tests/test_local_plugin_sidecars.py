"""Hybrid native services start only for an owned, selected local package."""
from types import SimpleNamespace
import asyncio
import pytest
from orchestration import local_plugin_sidecars as native, local_subprocess as host
from core.config.mcp_config import _mcp_http_url

@pytest.mark.asyncio
async def test_owned_bundled_server_is_supervised_once(monkeypatch):
    calls = []
    proc = SimpleNamespace(returncode=None, pid=99)
    import importlib
    config = importlib.import_module("core.config.settings")
    monkeypatch.setattr(config, "settings", SimpleNamespace(
        deploy=SimpleNamespace(is_local=True), server=SimpleNamespace(mcp_host="127.0.0.1")
    ))
    monkeypatch.setattr(native, "_lock", asyncio.Lock())
    monkeypatch.setattr(host, "_cloud_serves_tools", lambda: True)
    monkeypatch.setattr(host, "_WATCHDOG", None)
    monkeypatch.setattr(host, "_PROCS", [])
    monkeypatch.setattr(host, "_SPECS", {})
    async def port(*args): return False
    async def spawn(label, argv):
        calls.append((label, argv)); host._PROCS.append((label, proc)); return proc
    async def ready(*args, **kwargs): calls.append("ready")
    async def tools(*args): return {"browser_open"}
    monkeypatch.setattr(host, "_tcp_port_ready", port)
    monkeypatch.setattr(host, "_spawn", spawn)
    monkeypatch.setattr(host, "_wait_for_mcp_ports", ready)
    monkeypatch.setattr(host, "_list_mcp_tool_names", tools)
    monkeypatch.setattr(host, "_start_watchdog", lambda: calls.append("watchdog"))
    cfg = {"url": _mcp_http_url("browser_runtime"), "origin": "local_plugin",
           "execution_scope": "local", "owner_user_id": "owner"}
    await native.ensure_native_servers({"owned": cfg})
    owner_loop = asyncio.get_running_loop()
    monkeypatch.setattr(host, "_WATCHDOG", SimpleNamespace(get_loop=lambda: owner_loop))
    await asyncio.to_thread(lambda: asyncio.run(native.ensure_native_servers({"owned": cfg})))
    assert sum(isinstance(c, tuple) for c in calls) == 1
    assert calls[0][1][2] == "mcp_servers.browser_runtime_mcp.server"
    assert "ready" in calls and "watchdog" in calls
    for changed in [{"origin": "cloud"}, {"owner_user_id": None},
                    {"url": "https://third-party.example/mcp"}, {"execution_scope": "cloud"}]:
        assert not native.native_bindings({"ignored": {**cfg, **changed}})

    host._PROCS.clear()
    async def occupied(*args): return True
    monkeypatch.setattr(host, "_tcp_port_ready", occupied)
    with pytest.raises(RuntimeError, match="another process"):
        await native.ensure_native_servers({"owned": cfg})


@pytest.mark.asyncio
async def test_demand_spawn_and_watchdog_reuse_one_lifecycle_owner(monkeypatch):
    from orchestration import local_sidecar_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "_spawn_lock", asyncio.Lock())
    monkeypatch.setattr(host, "_PROCS", [])
    monkeypatch.setattr(host, "_SPECS", {})
    monkeypatch.setattr(host, "_SHUTTING_DOWN", False)
    monkeypatch.setattr(host, "_child_env", lambda: {})
    monkeypatch.setattr(host, "no_window_kwargs", lambda: {})
    spawned = []
    proc = SimpleNamespace(returncode=None, pid=55)
    async def create(*args, **kwargs):
        await asyncio.sleep(0)
        spawned.append(args)
        return proc
    monkeypatch.setattr(lifecycle.asyncio, "create_subprocess_exec", create)
    argv = ["python", "-m", "mcp_servers.browser_runtime_mcp.server"]
    # A demand activation during watchdog backoff is allowed to replace the
    # dead child. The watchdog's captured restart then reuses that same child.
    host._SPECS["plugin_mcp:browser_runtime"] = argv
    first, second = await asyncio.gather(
        lifecycle._spawn("plugin_mcp:browser_runtime", argv),
        lifecycle._spawn("plugin_mcp:browser_runtime", argv, require_registered=True),
    )
    assert first is second is proc and len(spawned) == 1
    host._SPECS.clear()
    assert await lifecycle._spawn("plugin_mcp:browser_runtime", argv, require_registered=True) is None
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_failed_start_cleanup_has_kill_fallback_and_revokes_restart(monkeypatch):
    from orchestration import local_sidecar_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "_spawn_lock", asyncio.Lock())
    calls = []
    class Child:
        returncode = None
        def terminate(self): calls.append("terminate")
        def kill(self): calls.append("kill"); self.returncode = -9
        async def wait(self): return self.returncode
    proc = Child()
    monkeypatch.setattr(host, "_PROCS", [("plugin_mcp:test", proc)])
    monkeypatch.setattr(host, "_SPECS", {"plugin_mcp:test": ["test"]})
    async def wait_for(awaitable, timeout):
        calls.append(timeout)
        if "kill" not in calls:
            awaitable.close()
            raise asyncio.TimeoutError
        return await awaitable
    monkeypatch.setattr(lifecycle.asyncio, "wait_for", wait_for)
    await lifecycle.discard("plugin_mcp:test", proc)
    assert calls == ["terminate", 5, "kill", 5]
    assert not host._PROCS and "plugin_mcp:test" not in host._SPECS


@pytest.mark.asyncio
async def test_shutdown_reaps_spawn_in_flight(monkeypatch):
    from orchestration import local_sidecar_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "_spawn_lock", asyncio.Lock())
    monkeypatch.setattr(host, "_PROCS", [])
    monkeypatch.setattr(host, "_SPECS", {})
    monkeypatch.setattr(host, "_SHUTTING_DOWN", False)
    monkeypatch.setattr(host, "_WATCHDOG", None)
    monkeypatch.setattr(host, "_child_env", lambda: {})
    monkeypatch.setattr(host, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(lifecycle.os, "name", "posix")
    entered, resume = asyncio.Event(), asyncio.Event()
    class Child:
        pid, returncode = 56, None
        def send_signal(self, signal): self.returncode = -15
        async def wait(self): return self.returncode
    proc = Child()
    async def create(*args, **kwargs):
        entered.set()
        await resume.wait()
        return proc
    monkeypatch.setattr(lifecycle.asyncio, "create_subprocess_exec", create)
    spawn = asyncio.create_task(lifecycle._spawn("pending", ["python"]))
    await entered.wait()
    shutdown = asyncio.create_task(lifecycle.stop_local_sidecars())
    await asyncio.sleep(0)
    assert host._SHUTTING_DOWN and not shutdown.done()
    resume.set()
    await asyncio.gather(spawn, shutdown)
    assert proc.returncode == -15 and not host._PROCS
    assert await lifecycle._spawn("pending", ["python"]) is None
