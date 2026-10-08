"""Start trusted bundled MCPs selected by owned local plugin definitions."""
from __future__ import annotations
import asyncio
import sys
from urllib.parse import urlsplit

_lock = asyncio.Lock()


def native_bindings(configs):
    from core.config.mcp_config import _mcp_http_url
    from core.config.settings import settings
    from mcp_servers._ports import PORTS

    if not settings.deploy.is_local:
        return set()
    urls = {
        _mcp_http_url(sid).rstrip("/"): (sid, port) for sid, port in PORTS.items()
        if urlsplit(_mcp_http_url(sid)).hostname in ("127.0.0.1", "localhost", "::1")
    }
    return {
        urls[str(cfg.get("url") or "").rstrip("/")]
        for cfg in configs.values()
        if cfg.get("origin") == "local_plugin"
        and cfg.get("execution_scope") == "local"
        and cfg.get("owner_user_id")
        and str(cfg.get("url") or "").rstrip("/") in urls
    }


async def ensure_native_servers(configs):
    from orchestration import local_subprocess as host

    if not host._cloud_serves_tools():
        return  # The local-only launcher already owns every bundled MCP.
    bindings = native_bindings(configs)
    if not bindings:
        return
    # Isolated agents execute on worker loops. Process handles, the watchdog
    # and the serialization lock remain on the backend's lifecycle loop.
    owner_loop = host._WATCHDOG.get_loop() if host._WATCHDOG is not None else asyncio.get_running_loop()
    if owner_loop is not asyncio.get_running_loop():
        future = asyncio.run_coroutine_threadsafe(_start(bindings), owner_loop)
        await asyncio.wrap_future(future)
        return
    await _start(bindings)


async def _start(bindings):
    from mcp_servers._ports import package_name
    from orchestration import local_subprocess as host

    async with _lock:
        for sid, port in sorted(bindings):
            label = "plugin_mcp:" + sid
            running = next((p for name, p in host._PROCS if name == label and p.returncode is None), None)
            if running is not None:
                continue
            if await host._tcp_port_ready("127.0.0.1", port):
                raise RuntimeError("local plugin MCP port is already owned by another process")
            proc = await host._spawn(label, [
                sys.executable, "-m", f"mcp_servers.{package_name(sid)}.server",
                "--transport", "streamable-http", "--port", str(port),
            ])
            if proc is None:
                raise RuntimeError("local plugin MCP could not start")
            try:
                await host._wait_for_mcp_ports(proc, {sid: port}, timeout=host._ready_timeout_seconds())
                await host._list_mcp_tool_names(sid, port)
            except BaseException:
                from orchestration.local_sidecar_lifecycle import discard

                await discard(label, proc)
                raise
            host._start_watchdog()
