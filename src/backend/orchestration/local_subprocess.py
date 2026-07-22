"""No-Docker local profile: in-process supervision of the MCP + sandbox sidecars.

In the compose deployment the MCP servers run in the dedicated ``mcp`` container
and the code-execution sidecar runs in ``script-runner``. The local/quick-install
profile has neither container — one backend process owns everything — so on
startup we spawn both as **child subprocesses** bound to loopback, and reap them
on shutdown. Only active when ``DEPLOY_PROFILE=local``; the compose path never
imports this module.

- MCP launcher: ``python -m mcp_servers._launcher`` — already self-supervises one
  streamable-http server per port (see ``mcp_servers/_launcher.py``); the backend
  reaches them at ``127.0.0.1:<port>`` (``MCP_HOST=127.0.0.1``).
- Script runner: the ``services/script_runner_service`` FastAPI app on
  ``127.0.0.1:8900`` (``SANDBOX_RUNNER_URL`` points here). Pure host subprocess
  executor — no container needed to run Python/bash.

These sidecars are part of the local product's readiness contract. Startup waits
for every registered MCP port and verifies the three default-plugin tool lists;
on failure the API lifespan aborts instead of reporting a misleading healthy
desktop service with missing tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from typing import List, Optional, Tuple

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

# (label, argv) for each managed child. argv[0] is the current interpreter.
_PROCS: List[Tuple[str, "asyncio.subprocess.Process"]] = []

# These are not optional conveniences in the local/desktop product: they back
# the three plugins installed on the first zero-state boot.  Keeping this
# contract independent from ``_ports.PORTS`` is deliberate — if a CE packaging
# overlay accidentally drops one registration (the historical site_publish
# failure), startup must fail visibly instead of declaring the backend ready
# while silently omitting the tool.
_REQUIRED_PLUGIN_MCP_TOOLS = {
    "automation_task": "list_scheduled_tasks",
    "skill_manager": "list_my_skills",
    "site_publish": "publish_site",
}


def _ready_timeout_seconds() -> float:
    raw = os.getenv("LOCAL_SIDECAR_READY_TIMEOUT_SECONDS", "30")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def _child_env() -> dict:
    """Env for children: inherit ours and force local-only MCP networking."""
    env = dict(os.environ)
    # Both the advertised host and the actual listener stay on loopback for the
    # single-machine profile.
    env["MCP_HOST"] = "127.0.0.1"
    env["MCP_BIND_HOST"] = "127.0.0.1"
    return env


async def _spawn(label: str, argv: List[str]) -> Optional["asyncio.subprocess.Process"]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=_child_env(),
            stdout=None,  # inherit — child logs stream to the backend console
            stderr=None,
        )
        _PROCS.append((label, proc))
        logger.info("local_sidecar_spawned", sidecar=label, pid=proc.pid)
        return proc
    except Exception as exc:  # noqa: BLE001 — normalized into readiness failure by caller
        logger.warning("local_sidecar_spawn_failed", sidecar=label, error=str(exc))
        return None


async def _tcp_port_ready(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=0.5,
        )
        del reader
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _wait_for_mcp_ports(
    launcher: "asyncio.subprocess.Process",
    ports: dict[str, int],
    *,
    timeout: float,
) -> None:
    """Wait until every launchable local MCP server is accepting connections."""
    deadline = time.monotonic() + timeout
    pending = dict(ports)
    while pending and time.monotonic() < deadline:
        if launcher.returncode is not None:
            raise RuntimeError(f"MCP 启动器提前退出（exit={launcher.returncode}）")
        checks = await asyncio.gather(
            *(_tcp_port_ready("127.0.0.1", port) for port in pending.values())
        )
        pending = {
            server_id: port
            for (server_id, port), ready in zip(pending.items(), checks)
            if not ready
        }
        if pending:
            await asyncio.sleep(0.2)
    if pending:
        details = ", ".join(f"{server_id}:{port}" for server_id, port in pending.items())
        raise RuntimeError(f"MCP 服务未在 {timeout:.0f} 秒内就绪：{details}")


async def _list_mcp_tool_names(server_id: str, port: int) -> set[str]:
    """Use the production MCP client to verify a server's actual tool list."""
    from core.llm.mcp_pool import make_client

    client = make_client(
        server_id,
        {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{port}/mcp/",
            "transport_timeout": 5,
        },
        is_stateful=False,
    )
    try:
        tools = await client.list_tools()
        return {str(name) for tool in tools if (name := getattr(tool, "name", None))}
    finally:
        try:
            await client.close()
        except asyncio.CancelledError:
            # Some MCP transports use cancellation internally while closing.
            # Suppress that implementation detail, but preserve cancellation of
            # the actual application startup task.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
        except Exception:  # noqa: BLE001 — readiness result is already known
            pass


async def _verify_required_plugin_tools(ports: dict[str, int]) -> None:
    """Assert that every default plugin MCP exposes its contract tool."""
    missing_registrations = set(_REQUIRED_PLUGIN_MCP_TOOLS) - set(ports)
    if missing_registrations:
        raise RuntimeError("默认插件缺少 MCP 端口注册：" + ", ".join(sorted(missing_registrations)))

    async def _verify(server_id: str, expected_tool: str) -> None:
        names = await _list_mcp_tool_names(server_id, ports[server_id])
        if expected_tool not in names:
            raise RuntimeError(f"MCP {server_id} 已监听但缺少必需工具 {expected_tool}")

    await asyncio.gather(
        *(
            _verify(server_id, expected_tool)
            for server_id, expected_tool in _REQUIRED_PLUGIN_MCP_TOOLS.items()
        )
    )


async def start_local_sidecars() -> None:
    """Spawn the MCP launcher + script_runner sidecar (local profile only)."""
    if not settings.deploy.is_local:
        return
    py = sys.executable or "python"

    from mcp_servers._launcher import PORTS as launcher_ports
    from mcp_servers._ports import PORTS as server_ports
    from mcp_servers._ports import package_name

    launchable_ports = {
        server_id: port
        for server_id, port in server_ports.items()
        if launcher_ports.get(package_name(server_id)) == port
    }
    missing_required = set(_REQUIRED_PLUGIN_MCP_TOOLS) - set(launchable_ports)
    if missing_required:
        raise RuntimeError(
            "默认插件 MCP 未进入本地启动清单：" + ", ".join(sorted(missing_required))
        )

    # 1) MCP launcher — one streamable-http server per port, self-supervised.
    launcher = await _spawn("mcp_launcher", [py, "-m", "mcp_servers._launcher"])
    if launcher is None:
        raise RuntimeError("无法启动 MCP 服务管理进程")

    # 2) Code-execution sidecar — host subprocess executor on 127.0.0.1:8900.
    #    Only start it when script_runner is the selected provider (default).
    if settings.sandbox.provider == "script_runner":
        runner = await _spawn(
            "script_runner",
            [
                py,
                "-m",
                "uvicorn",
                "services.script_runner_service.server:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8900",
                "--log-level",
                "warning",
            ],
        )
        if runner is None:
            await stop_local_sidecars()
            raise RuntimeError("无法启动本机代码执行服务")

    # Do not let uvicorn finish its lifespan (and therefore let the desktop
    # shell report /health as ready) until the sidecars behind the advertised
    # default plugins are genuinely usable.
    try:
        await _wait_for_mcp_ports(
            launcher,
            launchable_ports,
            timeout=_ready_timeout_seconds(),
        )
        await _verify_required_plugin_tools(launchable_ports)
        logger.info(
            "local_mcp_sidecars_ready",
            servers=len(launchable_ports),
            required_plugins=sorted(_REQUIRED_PLUGIN_MCP_TOOLS),
        )
    except BaseException:
        await stop_local_sidecars()
        raise


async def stop_local_sidecars() -> None:
    """Terminate managed children (SIGTERM, then SIGKILL) on shutdown."""
    if not _PROCS:
        return
    for label, proc in _PROCS:
        if proc.returncode is not None:
            continue
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("local_sidecar_term_failed", sidecar=label, error=str(exc))
    # Give them a moment, then hard-kill stragglers.
    for label, proc in _PROCS:
        if proc.returncode is not None:
            continue
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        logger.info("local_sidecar_stopped", sidecar=label)
    _PROCS.clear()
