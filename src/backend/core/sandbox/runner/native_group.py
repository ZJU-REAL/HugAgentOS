"""Linux owner process groups retain their unreaped leader until the whole group stops."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path


class NativeGroupProcess:
    def __init__(self, process, *, lifetime_confined=False):
        if not sys.platform.startswith("linux") or not hasattr(os, "waitid"):
            raise ValueError("Native process-group ownership unavailable")
        self.lifetime_confined = lifetime_confined
        self._process = process  # Strong Popen reference; never Popen.poll before group stop.
        self.pid = process.pid
        self._uid = os.stat(f"/proc/{process.pid}").st_uid
        self._started = int(
            Path(f"/proc/{process.pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
        )
        self._namespace_init = None
        self._lock = threading.RLock()
        self._reaped = False
        self._lost = False
        self._namespace_fd = None
        self._namespace_inode = None

    def _leader_exit(self):
        try:
            return os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            self._lost = True
            raise RuntimeError("Native group ownership lost") from None

    def pin_namespace(self):
        with self._lock:
            if not self.lifetime_confined:
                return False
            if self._namespace_fd is not None:
                return True
            host = os.stat("/proc/self/ns/pid").st_ino
            pending = [self.pid]
            seen = set()
            while pending and len(seen) < 256:
                pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    namespace = Path(f"/proc/{pid}/ns/pid")
                    status = Path(f"/proc/{pid}/status").read_text()
                    inner_pid = next(
                        (
                            line.split()[-1]
                            for line in status.splitlines()
                            if line.startswith("NSpid:")
                        ),
                        None,
                    )
                    if namespace.stat().st_ino != host and inner_pid == "1":
                        fd = os.open(namespace, os.O_RDONLY | os.O_CLOEXEC)
                        self._namespace_fd = fd
                        self._namespace_inode = os.fstat(fd).st_ino
                        self._namespace_init = pid
                        return True
                    pending.extend(
                        map(int, Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
                    )
                except (FileNotFoundError, ProcessLookupError):
                    continue
            return False

    def _related(self, pid):
        seen = set()
        while pid > 1 and pid not in seen and len(seen) < 256:
            if pid in {self.pid, self._namespace_init}:
                return True
            seen.add(pid)
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
                if int(fields[19]) < self._started:
                    return False
                pid = int(fields[1])
            except (FileNotFoundError, ProcessLookupError):
                return False
            except PermissionError:
                return True  # Unknown recent owned ancestry remains charged.
        return False

    def _live_members(self):
        # The unreaped leader pins PID/group identity during this scan and kill.
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
                if fields[0] in {"Z", "X"}:
                    continue
                if int(fields[2]) == self.pid:
                    return True
                # PID-lifetime user namespaces retain the launch UID mapping;
                # unrelated UID namespaces need not be ptrace-readable.
                if entry.stat().st_uid != self._uid:
                    continue
                if (
                    self._namespace_inode is not None
                    and (entry / "ns/pid").stat().st_ino == self._namespace_inode
                ):
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                try:
                    if entry.stat().st_uid != self._uid:
                        continue
                    if self._related(int(entry.name)):
                        return True
                except FileNotFoundError:
                    continue
                # Conclusively unrelated protected processes are outside this owner.
                continue
        return False

    def group_stopped(self):
        with self._lock:
            if self._reaped:
                return True
            return self._leader_exit() is not None and not self._live_members()

    @property
    def returncode(self):
        with self._lock:
            if self._reaped:
                return self._process.returncode
            if self._leader_exit() is None or self._live_members():
                return None
            # Whole group has no live members; native timers cannot race this reap.
            code = self._process.wait()
            self._reaped = True
            return code

    def signal_group(self, sig):
        with self._lock:
            if self._reaped:
                return
            self._leader_exit()  # Refuse numeric PID use if someone else reaped it.
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                pass

    def kill(self):
        self.signal_group(signal.SIGKILL)

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.02)
        return self.returncode

    def close(self):
        if not self.group_stopped():
            raise RuntimeError("Native group cleanup pending")
        self.returncode  # Reap only after verified whole-group/namespace exit.
        with self._lock:
            if self._namespace_fd is not None:
                os.close(self._namespace_fd)
                self._namespace_fd = None


def lifetime_command(cmd, trusted_prefix=()):
    """Preserve trusted existing confinement; otherwise add only PID lifetime isolation."""
    if (
        trusted_prefix
        and Path(trusted_prefix[0]).name == "bwrap"
        and "--unshare-pid" in trusted_prefix
        and "--die-with-parent" in trusted_prefix
    ):
        return cmd, True
    with process_state().namespace_lock:
        if process_state().namespace_prefix is None:
            executable = shutil.which("bwrap")
            prefix = (
                [
                    executable,
                    "--unshare-user",
                    "--unshare-pid",
                    "--die-with-parent",
                    "--dev-bind",
                    "/",
                    "/",
                    "--proc",
                    "/proc",
                    "--",
                ]
                if executable
                else []
            )
            if prefix:
                try:
                    probe = subprocess.run(
                        [*prefix, "/bin/true"],
                        timeout=5,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    if probe.returncode != 0:
                        prefix = []
                except (OSError, subprocess.TimeoutExpired):
                    prefix = []
            process_state().namespace_prefix = prefix
        prefix = list(process_state().namespace_prefix)
    return [*prefix, *cmd], bool(prefix)


@dataclass
class NativeGroupState:
    namespace_lock: object = field(default_factory=lambda: threading.Lock())
    namespace_prefix: object = None


_STATE = NativeGroupState()


def process_state() -> NativeGroupState:
    return _STATE
