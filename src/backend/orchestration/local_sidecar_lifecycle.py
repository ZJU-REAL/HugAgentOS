"""Lifecycle ownership shared by startup, lazy plugin activation and restart."""
from __future__ import annotations
import asyncio
import contextlib
import os
import signal
from typing import List, Optional
from orchestration import local_subprocess as host

_spawn_lock = asyncio.Lock()

async def _spawn(label: str, argv: List[str], *, require_registered=False) -> Optional["asyncio.subprocess.Process"]:
    async with _spawn_lock:
        if host._SHUTTING_DOWN or (require_registered and host._SPECS.get(label) != argv):
            return None
        running = next((p for name, p in host._PROCS if name == label and p.returncode is None), None)
        if running is not None:
            return running
        return await _create(label, argv)


async def _create(label, argv):
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=host._child_env(),
            stdout=None,  # inherit — child logs stream to the backend console
            stderr=None,
            **host.no_window_kwargs(),
        )
        host._PROCS.append((label, proc))
        host._SPECS[label] = list(argv)
        host.logger.info("local_sidecar_spawned", sidecar=label, pid=proc.pid)
        return proc
    except Exception as exc:  # noqa: BLE001 — normalized into readiness failure by caller
        host.logger.warning("local_sidecar_spawn_failed", sidecar=label, error=str(exc))
        return None


def _start_watchdog() -> None:
    """看门狗：sidecar 进程挂掉后自动重拉（此前无守护，runner 一死整个执行
    能力就停摆到重启应用为止）。带 10s 退避，关停期间不再拉起。"""
    if host._WATCHDOG is None or host._WATCHDOG.done():
        host._WATCHDOG = asyncio.get_event_loop().create_task(_supervise_sidecars())


async def _start_script_runner(py: str) -> None:
    """Code-execution sidecar — host subprocess executor on the branded loopback
    port from SANDBOX_RUNNER_URL. Only started when script_runner is the selected
    provider (default)."""
    if host.settings.sandbox.provider != "script_runner":
        return
    # 本机形态下 sidecar 监听的是回环口，同机任何进程都够得着——包括刚被 OS 沙箱
    # 关起来的那条命令（回环在沙箱里依然可达）。没有这个密钥，它再调一次 /processes/start
    # 就能拿到一个不带任何约束的子进程，等于从自己所在的沙箱里走出来。密钥经环境变量
    # 交给 sidecar，用户命令那一侧用的是白名单 env，拿不到它。
    os.environ[host.runner_auth.ENV_VAR] = host.runner_auth.ensure_token()
    runner_port = host._script_runner_port()
    # 清理后目标端口仍有监听者 → 是外部程序在占用。不同品牌的桌面壳
    # 应通过独立端口命名空间避免走到这里。
    # 若照常 spawn，自拉的 runner 会绑定失败退出，而下面的端口就绪检查
    # 会把占用者的应答当作启动成功——执行面被静默接到失控进程上。宁可
    # 启动失败并说清原因。
    if await host._tcp_port_ready("127.0.0.1", runner_port):
        owner = await host._describe_port_owner(runner_port)
        await stop_local_sidecars()
        raise RuntimeError(
            f"{runner_port} 端口已被其它程序占用（{owner or '占用者未知'}），"
            "无法启动本机代码执行服务——请退出占用该端口的程序后重启客户端"
        )
    runner = await host._spawn(
        "script_runner",
        [
            py,
            "-m",
            "uvicorn",
            "services.script_runner_service.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(runner_port),
            "--log-level",
            "warning",
        ],
    )
    if runner is None:
        await stop_local_sidecars()
        raise RuntimeError("无法启动本机代码执行服务")
    # 绑定失败时进程会立刻退出，
    # 但 _spawn 只看拉起瞬间——这里显式等端口就绪，问题在启动期暴露，
    # 而不是等到用户第一次执行命令才报「无法连接脚本执行服务」。
    runner_deadline = asyncio.get_event_loop().time() + host._ready_timeout_seconds()
    while not await host._tcp_port_ready("127.0.0.1", runner_port):
        if runner.returncode is not None:
            await stop_local_sidecars()
            raise RuntimeError(
                f"本机代码执行服务启动即退出（exit={runner.returncode}）——"
                "多为运行环境/依赖问题，请把安装日志末尾的报错反馈"
            )
        if asyncio.get_event_loop().time() > runner_deadline:
            await stop_local_sidecars()
            raise RuntimeError(
                f"本机代码执行服务未就绪（127.0.0.1:{runner_port}）——"
                "端口可能被其它程序占用"
            )
        await asyncio.sleep(0.5)


async def _supervise_sidecars() -> None:
    while True:
        await asyncio.sleep(20)
        if host._SHUTTING_DOWN:
            return
        for index, (label, proc) in enumerate(list(host._PROCS)):
            if proc.returncode is None:
                continue
            host.logger.warning(
                "local_sidecar_exited", sidecar=label, returncode=proc.returncode
            )
            try:
                host._PROCS.remove((label, proc))
            except ValueError:
                pass
            argv = host._SPECS.get(label)
            if not argv or host._SHUTTING_DOWN:
                continue
            await asyncio.sleep(10)
            replacement = await _spawn(label, argv, require_registered=True)
            if replacement is None:
                host.logger.warning("local_sidecar_respawn_failed", sidecar=label)


async def stop_local_sidecars() -> None:
    """Terminate managed children (SIGTERM, then SIGKILL) on shutdown."""
    host._SHUTTING_DOWN = True
    if host._WATCHDOG is not None:
        host._WATCHDOG.cancel()
        host._WATCHDOG = None
    async with _spawn_lock:
        if not host._PROCS:
            return
        for label, proc in host._PROCS:
            if proc.returncode is not None:
                continue
            if os.name == "nt":
                await host._terminate_process_trees([proc.pid])
                continue
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                continue
            except Exception as exc:  # noqa: BLE001
                host.logger.warning("local_sidecar_term_failed", sidecar=label, error=str(exc))
        # Give them a moment, then hard-kill stragglers.
        for label, proc in host._PROCS:
            if proc.returncode is not None:
                continue
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            host.logger.info("local_sidecar_stopped", sidecar=label)
        host._PROCS.clear()


async def discard(label, proc):
    """Bounded cleanup and ownership revocation serialized with restarts."""
    async with _spawn_lock:
        try:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5)
        finally:
            host._PROCS[:] = [(name, p) for name, p in host._PROCS if p is not proc]
            host._SPECS.pop(label, None)


def _child_env() -> dict:
    """Env for children: inherit ours and force local-only MCP networking."""
    env = dict(os.environ)
    # Both the advertised host and the actual listener stay on loopback for the
    # single-machine profile.
    env["MCP_HOST"] = "127.0.0.1"
    env["MCP_BIND_HOST"] = "127.0.0.1"
    inherited_pythonpath = [
        entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry
    ]
    pythonpath = [host._BACKEND_DIR]
    pythonpath.extend(entry for entry in inherited_pythonpath if entry != host._BACKEND_DIR)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return env
