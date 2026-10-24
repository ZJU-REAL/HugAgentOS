"""Own lifecycle and loop affinity for dynamically selected bundled sidecars."""
import asyncio
import sys

_lock = asyncio.Lock()

async def ensure_dynamic_servers(bindings):
    import core.infra.local_runtime.startup as host

    if not host._cloud_serves_tools():
        return  # The local-only launcher already owns every bundled MCP.
    if not bindings:
        return
    # Isolated agents execute on worker loops. Process handles, the watchdog
    # and the serialization lock remain on the backend's lifecycle loop.
    owner_loop = (
        host._WATCHDOG.get_loop() if host._WATCHDOG is not None else asyncio.get_running_loop()
    )
    if owner_loop is not asyncio.get_running_loop():
        future = asyncio.run_coroutine_threadsafe(_start(bindings), owner_loop)
        await asyncio.wrap_future(future)
        return
    await _start(bindings)


async def _start(bindings):
    import core.infra.local_runtime.startup as host
    from extensions.mcp_servers._ports import package_name

    async with _lock:
        for sid, port in sorted(bindings):
            label = "plugin_mcp:" + sid
            running = next(
                (p for name, p in host._PROCS if name == label and p.returncode is None), None
            )
            if running is not None:
                continue
            if await host._tcp_port_ready("127.0.0.1", port):
                raise RuntimeError("local plugin MCP port is already owned by another process")
            proc = await host._spawn(
                label,
                [
                    sys.executable,
                    "-m",
                    f"extensions.mcp_servers.{package_name(sid)}.server",
                    "--transport",
                    "streamable-http",
                    "--port",
                    str(port),
                ],
            )
            if proc is None:
                raise RuntimeError("local plugin MCP could not start")
            try:
                await host._wait_for_mcp_ports(
                    proc, {sid: port}, timeout=host._ready_timeout_seconds()
                )
                await host._list_mcp_tool_names(sid, port)
            except BaseException:
                from core.infra.local_runtime.lifecycle import discard

                await discard(label, proc)
                raise
            host._start_watchdog()
