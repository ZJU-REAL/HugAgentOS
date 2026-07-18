"""Entry point for the dedicated `mcp` container.

Spawns one streamable-http subprocess per MCP server on its assigned port,
prefixes their stdout/stderr with ``[<server>]`` for log readability,
restarts crashed children with exponential backoff, and exits non-zero if
any child crashes more than ``_CRASH_LIMIT`` times within ``_CRASH_WINDOW``
seconds (Docker then restarts the whole container).

Run with::

    python -m mcp_servers._launcher

Inside ``docker/Dockerfile.mcp`` this is the ``CMD``.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

from mcp_servers._ports import PORTS as _SERVER_PORTS, package_name


def _package_available(pkg: str) -> bool:
    """CE 派生树会物理排除 EE 专属 MCP 包（如 security_ops_mcp）；缺包时跳过
    而不是反复 spawn-crash 触发 crash-loop 守卫拖垮整个容器。"""
    from importlib.util import find_spec

    try:
        return find_spec(f"mcp_servers.{pkg}.server") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


_ALL_PKG_PORTS: Dict[str, int] = {
    package_name(server_id): port for server_id, port in _SERVER_PORTS.items()
}
PORTS: Dict[str, int] = {
    pkg: port for pkg, port in _ALL_PKG_PORTS.items() if _package_available(pkg)
}

_MISSING = set(_ALL_PKG_PORTS) - set(PORTS)
if _MISSING:
    print(f"[launcher] skipping absent MCP packages: {sorted(_MISSING)}", flush=True)

# Crash-loop guard: if any child crashes more than _CRASH_LIMIT times in
# _CRASH_WINDOW seconds, exit non-zero so docker restarts the container.
_CRASH_LIMIT = 5
_CRASH_WINDOW = 60.0
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0
_POLL_INTERVAL = 2.0
_GRACE_PERIOD = 10.0  # SIGTERM → SIGKILL grace


class _Child:
    __slots__ = ("server_id", "port", "proc", "backoff", "crash_times", "_pump_thread")

    def __init__(self, server_id: str, port: int) -> None:
        self.server_id = server_id
        self.port = port
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.backoff = _BACKOFF_INITIAL
        self.crash_times: List[float] = []
        self._pump_thread: Optional[threading.Thread] = None

    @property
    def label(self) -> str:
        return f"[{self.server_id}]"

    def start(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            f"mcp_servers.{self.server_id}.server",
            "--transport",
            "streamable-http",
            "--port",
            str(self.port),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            close_fds=True,
        )
        self.proc = proc
        self._pump_thread = threading.Thread(
            target=self._pump_output,
            args=(proc,),
            name=f"pump-{self.server_id}",
            daemon=True,
        )
        self._pump_thread.start()
        print(
            f"{self.label} started pid={proc.pid} port={self.port}",
            file=sys.stdout,
            flush=True,
        )

    def _pump_output(self, proc: "subprocess.Popen[bytes]") -> None:
        stream = proc.stdout
        if stream is None:
            return
        prefix = self.label.encode()
        try:
            for raw in iter(stream.readline, b""):
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                sys.stdout.buffer.write(prefix + b" " + line + b"\n")
                sys.stdout.flush()
        except Exception:
            pass

    def record_crash(self) -> None:
        now = time.monotonic()
        # Drop crashes outside the rolling window
        self.crash_times = [t for t in self.crash_times if now - t <= _CRASH_WINDOW]
        self.crash_times.append(now)

    def crashed_too_often(self) -> bool:
        return len(self.crash_times) > _CRASH_LIMIT

    def bump_backoff(self) -> float:
        delay = self.backoff
        self.backoff = min(self.backoff * 2.0, _BACKOFF_MAX)
        return delay

    def reset_backoff(self) -> None:
        self.backoff = _BACKOFF_INITIAL


def _terminate_all(children: List[_Child]) -> None:
    """Send SIGTERM to all children, wait up to _GRACE_PERIOD, then SIGKILL."""
    for child in children:
        if child.proc is None:
            continue
        if child.proc.poll() is None:
            try:
                child.proc.terminate()
            except Exception:
                pass

    deadline = time.monotonic() + _GRACE_PERIOD
    while time.monotonic() < deadline:
        if all(c.proc is None or c.proc.poll() is not None for c in children):
            return
        time.sleep(0.2)

    for child in children:
        if child.proc is None or child.proc.poll() is not None:
            continue
        try:
            child.proc.kill()
        except Exception:
            pass


def main() -> int:
    children = [_Child(server_id, port) for server_id, port in PORTS.items()]
    shutdown = threading.Event()

    def _on_signal(signum: int, _frame: object) -> None:  # noqa: ARG001
        print(f"[launcher] received signal {signum}, shutting down", flush=True)
        shutdown.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    for child in children:
        try:
            child.start()
        except Exception as exc:
            print(f"[launcher] failed to start {child.server_id}: {exc}", flush=True)
            child.record_crash()

    try:
        while not shutdown.is_set():
            time.sleep(_POLL_INTERVAL)
            if shutdown.is_set():
                break

            for child in children:
                if child.proc is None:
                    # Initial start failed earlier — try again
                    try:
                        child.start()
                    except Exception as exc:
                        print(
                            f"{child.label} restart failed: {exc}",
                            flush=True,
                        )
                        child.record_crash()
                        if child.crashed_too_often():
                            print(
                                f"[launcher] {child.server_id} crashed > "
                                f"{_CRASH_LIMIT} times in {_CRASH_WINDOW}s, exiting",
                                flush=True,
                            )
                            shutdown.set()
                            break
                    continue

                rc = child.proc.poll()
                if rc is None:
                    # Child still running — heuristic: been alive >30s? trim window.
                    # No need to reset backoff aggressively; the next crash records
                    # itself in the rolling window naturally.
                    continue

                # Child exited
                child.record_crash()
                print(
                    f"{child.label} exited rc={rc}; "
                    f"crashes_in_window={len(child.crash_times)}",
                    flush=True,
                )
                if child.crashed_too_often():
                    print(
                        f"[launcher] {child.server_id} crashed > "
                        f"{_CRASH_LIMIT} times in {_CRASH_WINDOW}s, exiting",
                        flush=True,
                    )
                    shutdown.set()
                    break

                delay = child.bump_backoff()
                print(
                    f"{child.label} restarting in {delay:.1f}s",
                    flush=True,
                )
                # Don't sleep here while holding the loop — schedule via a
                # shorter sleep loop so signals stay responsive.
                slept = 0.0
                while slept < delay and not shutdown.is_set():
                    time.sleep(min(0.5, delay - slept))
                    slept += 0.5
                if shutdown.is_set():
                    break
                child.proc = None  # force start() to spawn fresh

    finally:
        _terminate_all(children)

    return 1 if any(c.crashed_too_often() for c in children) else 0


if __name__ == "__main__":
    sys.exit(main())
