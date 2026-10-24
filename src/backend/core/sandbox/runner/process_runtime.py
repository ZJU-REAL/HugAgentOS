"""One runner execution/confinement boundary, shared by native and async managed commands."""

import asyncio
import os
import signal
import subprocess
from typing import Any, Dict, Optional


def install(server):

    async def _spawn_subprocess(cmd, cwd, sandbox_launch, files, *, native_group=False):
        """One confinement/resource boundary for one-shot and managed commands."""
        nproc_limit = server._subprocess_nproc_limit(cmd)
        env = dict(server.SAFE_ENV)
        # Off the event loop: the first command in a user's folder reads their
        # login-shell environment, which can take seconds.
        env.update(await asyncio.to_thread(server._workspace_rules().subprocess_environment, cwd))
        spawn_plan = None
        if sandbox_launch is not None:
            cmd = [*sandbox_launch.argv_prefix, *cmd]
            env.update(sandbox_launch.env)
            spawn_plan = sandbox_launch.spawn_plan

        def _set_limits():
            # Keep the post-fork callback minimal: non-async-safe Python work in a
            # multi-threaded server's preexec_fn can deadlock before exec().
            if server.resource is not None and nproc_limit is not None:
                server.resource.setrlimit(server.resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))

        if os.name == "nt":
            # 桌面本机模式：服务自身没有控制台，被执行的命令若不显式禁用，会为每次
            # 执行新开一个黑色 cmd 窗口。
            spawn_options: Dict[str, Any] = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            }
        else:
            spawn_options = {
                # Host-local quick installs intentionally pass no preexec_fn at all.
                "preexec_fn": _set_limits if nproc_limit is not None else None,
                # Give every execution its own process group for descendant cleanup.
                "start_new_session": True,
            }

        stdin_file, stdout_file, stderr_file = files
        if spawn_plan is not None:
            return server._spawn_with_sandbox_plan(
                spawn_plan,
                cmd,
                cwd=cwd,
                env=env,
                files=files,
            )
        if native_group:
            from .native_group import NativeGroupProcess, lifetime_command

            cmd, lifetime_confined = lifetime_command(
                cmd, sandbox_launch.argv_prefix if sandbox_launch is not None else ()
            )
            return NativeGroupProcess(
                subprocess.Popen(
                    cmd,
                    stdin=stdin_file,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=cwd,
                    env=env,
                    **spawn_options,
                ),
                lifetime_confined=lifetime_confined,
            )
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin_file,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            env=env,
            **spawn_options,
        )

    def _spawn_with_sandbox_plan(plan: Dict[str, Any], cmd: list, *, cwd, env, files):
        """Start a command under the Windows sandbox plan the backend decided on.

        A plan we cannot apply is an error, never a reason to start the command
        anyway: the layers above have already told the user this command is
        confined. The import is local because it is only reachable on Windows local
        mode, where the full backend tree is on the path — the Docker image ships
        this directory alone and never produces a plan to begin with.
        """
        from core.sandbox.oslayer.windows_runtime import spawn_confined

        stdin_file, stdout_file, stderr_file = files
        return spawn_confined(
            plan,
            cmd,
            cwd=cwd,
            env=env,
            stdin=stdin_file.fileno(),
            stdout=stdout_file.fileno(),
            stderr=stderr_file.fileno(),
        )

    def _subprocess_nproc_limit(cmd: list) -> Optional[int]:
        """Return the child limit that is safe for the selected deployment profile.

        Linux accounts ``RLIMIT_NPROC`` against the process' real UID, not against
        the child or its process tree.  The no-Docker quick-install profile shares
        its UID with the backend, MCP sidecars, desktop session, and every other
        process owned by the user.  Setting a limit of 64/128 there makes a child
        start successfully but prevents bash from forking as soon as the user's
        *total* process count reaches the limit.  Docker deployments have their own
        UID namespace plus a cgroup ``pids_limit``, so retain the defence in depth
        there and skip only the unsafe host-local limit.
        """
        if server.resource is None or os.name == "nt":
            return None
        if os.getenv("DEPLOY_PROFILE", "").strip().lower() == "local":
            return None

        # Do not limit RLIMIT_AS (virtual address space): mmap-ing .so shared libraries
        # needs lots of virtual address space; 256MB makes C extensions like lxml/numpy
        # fail with "failed to map segment from shared object".
        # Do not limit RLIMIT_FSIZE: internal file operations during .NET runtime startup trigger SIGXFSZ.
        # Actual disk usage is controlled at the container level by Docker tmpfs size and mem_limit.
        return 128 if cmd and cmd[0] in {"node", "bash"} else 64

    async def _terminate_process_group(
        proc: Optional[Any],
    ) -> None:
        """Kill and reap one execution process together with all descendants.

        Works against either an asyncio child or the token-backed process a Windows
        sandbox plan produces: both expose ``pid`` / ``returncode`` / ``kill`` /
        ``wait``, which is the whole surface used here.
        """
        if proc is None:
            return
        if hasattr(proc, "group_stopped"):
            proc.kill()  # Exact unreaped group pin; never signal a recyclable numeric PID.
            await proc.wait()
            return
        if os.name == "nt":
            # Once the leader has exited Windows may immediately recycle its PID;
            # taskkill on that stale PID could target an unrelated process. Timeout
            # and cancellation reach this branch while the leader is still alive.
            if proc.returncode is not None:
                return
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (FileNotFoundError, OSError):
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (AttributeError, PermissionError):
            # Defensive fallback for unusual POSIX runtimes where process-group
            # signalling is unavailable even though this service uses ``resource``.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        if proc.returncode is None:
            await server._wait_for_process_exit(proc)

    async def _wait_for_process_exit(proc: Any) -> int:
        """Wait until asyncio's child watcher has reaped the subprocess.

        ``Process.wait()`` has a race on some local quick-install runtimes when a
        very short-lived shell exits between waiter registration and the transport
        callback: ``returncode`` is already populated, yet the waiter is never
        resolved.  Polling the child-watcher-owned return code avoids that false
        timeout without doing our own ``waitpid`` or blocking the event loop.
        """
        while proc.returncode is None:
            await asyncio.sleep(0.02)
        return proc.returncode

    server._spawn_subprocess = _spawn_subprocess
    server._spawn_with_sandbox_plan = _spawn_with_sandbox_plan
    server._subprocess_nproc_limit = _subprocess_nproc_limit
    server._terminate_process_group = _terminate_process_group
    server._wait_for_process_exit = _wait_for_process_exit
