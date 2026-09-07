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
- Script runner: the ``services/script_runner_service`` FastAPI app on the
  loopback port configured by ``SANDBOX_RUNNER_URL``. Pure host subprocess
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
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from core.config.settings import settings
from core.infra.logging import get_logger
from core.infra.proc import no_window_kwargs

logger = get_logger(__name__)

# (label, argv) for each managed child. argv[0] is the current interpreter.
_PROCS: List[Tuple[str, "asyncio.subprocess.Process"]] = []
# label → argv：看门狗按此重拉挂掉的 sidecar。
_SPECS: dict = {}
_WATCHDOG: "asyncio.Task | None" = None
_SHUTTING_DOWN = False

# Child interpreters are launched with ``python -m`` rather than through the
# backend CLI file.  Python therefore searches their working directory instead
# of automatically adding ``src/backend`` (the CLI script's directory) to
# ``sys.path``.  Keep the packaged source root explicit so the desktop runtime
# can import ``mcp_servers`` and ``services`` on every platform.
_BACKEND_DIR = str(Path(__file__).resolve().parents[1])

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


# ── 遗留孤儿 sidecar 清理（打包桌面形态，POSIX） ─────────────────────────────
# App 非正常退出（强退/崩溃/更新中断）时 sidecar 可能失管存活并继续占用端口。
# 新会话若直接复用孤儿有两个坑：一是孤儿跑的还是旧 release 的代码；二是 macOS
# 上其 TCC 责任进程已死，tccd 把访问记到裸 python 头上——访问 下载/文稿/桌面 等
# 受保护目录被静默拒绝且不弹授权窗，`tccutil reset <bundle-id>` 也重置不到。
_SIDECAR_CMD_MARKERS = ("-m mcp_servers.", "services.script_runner_service.server:app")


def _packaged_install_root() -> Optional[str]:
    """打包桌面形态的安装根指纹；开发形态返回 None（跳过清理）。

    打包形态下解释器路径形如
    ``<config_dir>/local-server/releases/<hash>/venv/bin/python``——取到
    ``local-server`` 为止的前缀，跨 release 目录稳定，且能与同机其它产品
    （不同 config_dir）的本机面区分开。
    """
    exe = sys.executable or ""
    seg = f"{os.sep}local-server{os.sep}"
    idx = exe.find(seg)
    if idx == -1:
        return None
    return exe[: idx + len(seg) - 1]


def _find_stale_sidecar_pids(
    ps_output: str, *, root_marker: str, my_pid: int, fold_case: bool = False
) -> List[int]:
    """从 ``<pid> <command line>`` 表里挑出本安装根的遗留 sidecar 进程。

    双重匹配：命令行须同时含本安装根路径 + sidecar 模块指纹，避免误杀
    其它产品/其它安装的进程。Windows 的路径大小写不稳定，按 ``fold_case``
    折叠比较。纯函数，便于单测。
    """
    pids: List[int] = []
    marker = root_marker.casefold() if fold_case else root_marker
    for line in ps_output.splitlines():
        entry = line.strip()
        if not entry:
            continue
        pid_text, _, cmd = entry.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == my_pid:
            continue
        if marker not in (cmd.casefold() if fold_case else cmd):
            continue
        if not any(marker in cmd for marker in _SIDECAR_CMD_MARKERS):
            continue
        pids.append(pid)
    return pids


async def _process_table() -> str:
    """``<pid> <command line>`` 一行一个进程。Windows 没有 ps，走 CIM。"""
    if os.name == "nt":
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process | ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.CommandLine }",
        ]
    else:
        argv = ["/bin/ps", "-axo", "pid=,command="]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **no_window_kwargs(),
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace")


async def _describe_port_owner(port: int) -> str:
    """占用 loopback 端口的进程描述（``pid name``），查不到时返回空串。"""
    try:
        if os.name == "nt":
            proc = await asyncio.create_subprocess_exec(
                "netstat.exe",
                "-ano",
                "-p",
                "tcp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **no_window_kwargs(),
            )
            out, _ = await proc.communicate()
            pid = _listener_pid_from_netstat(out.decode("utf-8", "replace"), port)
        else:
            proc = await asyncio.create_subprocess_exec(
                "lsof",
                "-nP",
                "-t",
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            first = out.decode("utf-8", "replace").split()
            pid = int(first[0]) if first else None
        if pid is None:
            return ""
        table = await _process_table()
        for line in table.splitlines():
            pid_text, _, cmd = line.strip().partition(" ")
            if pid_text == str(pid):
                return f"PID {pid}：{cmd.strip()[:160]}"
        return f"PID {pid}"
    except Exception:  # noqa: BLE001 — 诊断信息缺失不改变失败本身
        return ""


def _listener_pid_from_netstat(output: str, port: int) -> Optional[int]:
    """从 ``netstat -ano`` 输出里取 loopback/任意地址上该端口的监听进程。"""
    suffix = f":{port}"
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP":
            continue
        if not fields[1].endswith(suffix) or fields[3].upper() != "LISTENING":
            continue
        try:
            return int(fields[4])
        except ValueError:
            continue
    return None


async def _terminate_process_trees(pids: List[int]) -> None:
    """Windows 没有 SIGTERM；结束进程时连子进程树一起结束，MCP launcher 的
    子服务才不会变成下一次启动要清理的孤儿。"""
    for pid in pids:
        proc = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **no_window_kwargs(),
        )
        await proc.wait()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _reap_stale_sidecars() -> None:
    """启动期一次性清掉上次会话遗留的孤儿 sidecar，再拉本会话自己的。

    两轮扫描：孤儿 MCP launcher 被 SIGTERM 时可能正好重拉子进程，第二轮兜住。
    结束后等当前品牌的 runner 端口释放，保证随后的自拉 runner 能绑定成功。
    """
    # 桌面壳只按记录的 PID 回收进程树；服务非正常退出后遗留的 runner / MCP
    # 子进程壳不知道，所以 Windows 同样要在这里按安装根扫一遍。
    root = _packaged_install_root()
    if not root:
        return
    reaped = 0
    for _attempt in range(2):
        try:
            table = await _process_table()
        except Exception as exc:  # noqa: BLE001 — 清理失败只降级告警，不阻断启动
            logger.warning("stale_sidecar_scan_failed", error=str(exc))
            return
        victims = _find_stale_sidecar_pids(
            table, root_marker=root, my_pid=os.getpid(), fold_case=os.name == "nt"
        )
        if not victims:
            break
        logger.warning("stale_sidecars_found", pids=victims, attempt=_attempt + 1)
        if os.name == "nt":
            await _terminate_process_trees(victims)
            reaped += len(victims)
            continue
        for pid in victims:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and any(_pid_alive(p) for p in victims):
            await asyncio.sleep(0.2)
        for pid in victims:
            if _pid_alive(pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        reaped += len(victims)
    if reaped and settings.sandbox.provider == "script_runner":
        runner_port = _script_runner_port()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and await _tcp_port_ready(
            "127.0.0.1", runner_port
        ):
            await asyncio.sleep(0.2)
        logger.info("stale_sidecars_reaped", count=reaped)


def _script_runner_port() -> int:
    """Return the local runner port from the already-resolved settings URL."""

    raw = settings.sandbox.runner_url
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"SANDBOX_RUNNER_URL 无效：{raw!r}") from exc
    # The managed uvicorn child binds plain HTTP on IPv4 loopback.
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise RuntimeError(
            "本机模式 SANDBOX_RUNNER_URL 必须指向 loopback：http://127.0.0.1:<port>"
        )
    if port is None or port == 0:
        raise RuntimeError(f"SANDBOX_RUNNER_URL 缺少端口：{raw!r}")
    return port


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
    inherited_pythonpath = [
        entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry
    ]
    pythonpath = [_BACKEND_DIR]
    pythonpath.extend(entry for entry in inherited_pythonpath if entry != _BACKEND_DIR)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return env


async def _spawn(label: str, argv: List[str]) -> Optional["asyncio.subprocess.Process"]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=_child_env(),
            stdout=None,  # inherit — child logs stream to the backend console
            stderr=None,
            **no_window_kwargs(),
        )
        _PROCS.append((label, proc))
        _SPECS[label] = list(argv)
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


def _cloud_serves_tools() -> bool:
    """混合模式（桌面壳注入了桥接密钥）下，工具全部来自云端 capability manifest：
    本机执行面只负责本地项目的代码执行，不运行任何内置 MCP。"""
    from core.auth.desktop_bridge import bridge_enabled

    return bridge_enabled()


def _start_watchdog() -> None:
    """看门狗：sidecar 进程挂掉后自动重拉（此前无守护，runner 一死整个执行
    能力就停摆到重启应用为止）。带 10s 退避，关停期间不再拉起。"""
    global _WATCHDOG
    if _WATCHDOG is None or _WATCHDOG.done():
        _WATCHDOG = asyncio.get_event_loop().create_task(_supervise_sidecars())


async def _start_script_runner(py: str) -> None:
    """Code-execution sidecar — host subprocess executor on the branded loopback
    port from SANDBOX_RUNNER_URL. Only started when script_runner is the selected
    provider (default)."""
    if settings.sandbox.provider != "script_runner":
        return
    runner_port = _script_runner_port()
    # 清理后目标端口仍有监听者 → 是外部程序在占用。不同品牌的桌面壳
    # 应通过独立端口命名空间避免走到这里。
    # 若照常 spawn，自拉的 runner 会绑定失败退出，而下面的端口就绪检查
    # 会把占用者的应答当作启动成功——执行面被静默接到失控进程上。宁可
    # 启动失败并说清原因。
    if await _tcp_port_ready("127.0.0.1", runner_port):
        owner = await _describe_port_owner(runner_port)
        await stop_local_sidecars()
        raise RuntimeError(
            f"{runner_port} 端口已被其它程序占用（{owner or '占用者未知'}），"
            "无法启动本机代码执行服务——请退出占用该端口的程序后重启客户端"
        )
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
    runner_deadline = asyncio.get_event_loop().time() + _ready_timeout_seconds()
    while not await _tcp_port_ready("127.0.0.1", runner_port):
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


async def start_local_sidecars() -> None:
    """Spawn the script_runner sidecar, plus the MCP launcher for local-only installs."""
    global _SHUTTING_DOWN
    if not settings.deploy.is_local:
        return
    _SHUTTING_DOWN = False
    py = sys.executable or "python"
    # 先清掉上次会话遗留的孤儿 sidecar，避免下面的端口就绪检查把孤儿当作
    # 自己拉起的进程「收养」（孤儿跑旧代码 + macOS TCC 归因已断裂）。
    await _reap_stale_sidecars()

    if _cloud_serves_tools():
        await _start_script_runner(py)
        _start_watchdog()
        logger.info("local_sidecars_ready", mode="cloud_tools", sidecars=[label for label, _ in _PROCS])
        return

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

    await _start_script_runner(py)

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

    _start_watchdog()


async def _supervise_sidecars() -> None:
    while True:
        await asyncio.sleep(20)
        if _SHUTTING_DOWN:
            return
        for index, (label, proc) in enumerate(list(_PROCS)):
            if proc.returncode is None:
                continue
            logger.warning(
                "local_sidecar_exited", sidecar=label, returncode=proc.returncode
            )
            try:
                _PROCS.remove((label, proc))
            except ValueError:
                pass
            argv = _SPECS.get(label)
            if not argv or _SHUTTING_DOWN:
                continue
            await asyncio.sleep(10)
            replacement = await _spawn(label, argv)
            if replacement is None:
                logger.warning("local_sidecar_respawn_failed", sidecar=label)


async def stop_local_sidecars() -> None:
    """Terminate managed children (SIGTERM, then SIGKILL) on shutdown."""
    global _SHUTTING_DOWN, _WATCHDOG
    _SHUTTING_DOWN = True
    if _WATCHDOG is not None:
        _WATCHDOG.cancel()
        _WATCHDOG = None
    if not _PROCS:
        return
    for label, proc in _PROCS:
        if proc.returncode is not None:
            continue
        if os.name == "nt":
            await _terminate_process_trees([proc.pid])
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
