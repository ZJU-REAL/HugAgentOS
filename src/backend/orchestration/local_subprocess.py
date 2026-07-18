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

These are best-effort: a spawn failure is logged and the backend still serves
(tools that need the missing sidecar degrade to an error to the model, main loop
unaffected).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import List, Optional, Tuple

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

# (label, argv) for each managed child. argv[0] is the current interpreter.
_PROCS: List[Tuple[str, "asyncio.subprocess.Process"]] = []


def _child_env() -> dict:
    """Env for children: inherit ours, pin loopback host defaults."""
    env = dict(os.environ)
    # MCP servers still bind 0.0.0.0 inside their own process; the backend reaches
    # them via MCP_HOST. Pin it to loopback for the single-machine profile.
    env.setdefault("MCP_HOST", "127.0.0.1")
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
    except Exception as exc:  # noqa: BLE001 — best-effort, never block startup
        logger.warning("local_sidecar_spawn_failed", sidecar=label, error=str(exc))
        return None


async def start_local_sidecars() -> None:
    """Spawn the MCP launcher + script_runner sidecar (local profile only)."""
    if not settings.deploy.is_local:
        return
    py = sys.executable or "python"

    # 1) MCP launcher — one streamable-http server per port, self-supervised.
    await _spawn("mcp_launcher", [py, "-m", "mcp_servers._launcher"])

    # 2) Code-execution sidecar — host subprocess executor on 127.0.0.1:8900.
    #    Only start it when script_runner is the selected provider (default).
    if settings.sandbox.provider == "script_runner":
        await _spawn(
            "script_runner",
            [
                py, "-m", "uvicorn",
                "services.script_runner_service.server:app",
                "--host", "127.0.0.1",
                "--port", "8900",
                "--log-level", "warning",
            ],
        )


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
