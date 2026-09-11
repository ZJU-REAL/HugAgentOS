"""Starting a process under a restricted token, and waiting for it.

``subprocess`` cannot attach a token to a child, so the launch goes through
``CreateProcessAsUserW`` directly. The returned object exposes the small slice
of :class:`asyncio.subprocess.Process` its caller needs — ``pid``,
``returncode``, ``kill()`` and ``wait()`` — so the runner's existing timeout and
descendant-cleanup code works against it unchanged.

Two things are deliberate for a long-lived caller. The child's environment is
passed explicitly rather than inherited, because a server must not rewrite its
own ``TEMP`` in order to redirect one command's scratch space. And only the
three stdio handles are made inheritable, matching what ``subprocess`` itself
does, so a concurrent request's handles cannot travel into this child.
"""

from __future__ import annotations

import asyncio
import ctypes
import msvcrt
import os
import subprocess
from ctypes import wintypes

from . import ffi

_WAIT_OBJECT_0 = 0x00000000
_KILLED_EXIT_CODE = 1


def _environment_block(env: dict[str, str]) -> ctypes.Array:
    """A ``CREATE_UNICODE_ENVIRONMENT`` block: ``K=V\\0`` … terminated by ``\\0``."""
    parts = "".join(f"{key}={value}\0" for key, value in env.items())
    return ctypes.create_unicode_buffer(parts + "\0")


class TokenProcess:
    """A process running under a restricted token.

    ``returncode`` is read from the OS on demand rather than delivered by a
    callback, so a caller that polls it — as the runner does — sees the exit as
    soon as it happens without needing a watcher of its own.
    """

    def __init__(self, handle: wintypes.HANDLE, thread: wintypes.HANDLE, pid: int) -> None:
        self._handle = handle
        self._thread = thread
        self.pid = pid
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        if self._returncode is None and self._handle:
            if ffi.WaitForSingleObject(self._handle, 0) == _WAIT_OBJECT_0:
                code = wintypes.DWORD()
                ffi.check(
                    ffi.GetExitCodeProcess(self._handle, ctypes.byref(code)),
                    "GetExitCodeProcess",
                )
                self._returncode = int(code.value)
        return self._returncode

    def kill(self) -> None:
        if self._handle and self.returncode is None:
            ffi.TerminateProcess(self._handle, _KILLED_EXIT_CODE)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.02)
        return self._returncode or 0

    def close(self) -> None:
        """Release the process and thread handles. Safe to call more than once."""
        for handle in (self._thread, self._handle):
            if handle:
                ffi.CloseHandle(handle)
        self._thread = None
        self._handle = None


def spawn_with_token(
    token: wintypes.HANDLE,
    command: list[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    stdin: int,
    stdout: int,
    stderr: int,
) -> TokenProcess:
    """Start ``command`` under ``token``; the three stdio arguments are fds."""
    if not command:
        raise ValueError("受限令牌启动缺少要执行的命令")

    handles = [msvcrt.get_osfhandle(fd) for fd in (stdin, stdout, stderr)]
    for handle in handles:
        os.set_handle_inheritable(handle, True)

    startup = ffi.STARTUPINFOW()
    startup.cb = ctypes.sizeof(ffi.STARTUPINFOW)
    startup.dwFlags = ffi.STARTF_USESTDHANDLES
    startup.hStdInput, startup.hStdOutput, startup.hStdError = handles

    information = ffi.PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    ffi.check(
        ffi.CreateProcessAsUserW(
            token,
            None,
            command_line,
            None,
            None,
            True,
            ffi.CREATE_UNICODE_ENVIRONMENT | ffi.CREATE_NO_WINDOW | ffi.CREATE_NEW_PROCESS_GROUP,
            _environment_block(env),
            cwd,
            ctypes.byref(startup),
            ctypes.byref(information),
        ),
        "CreateProcessAsUserW",
    )
    return TokenProcess(information.hProcess, information.hThread, int(information.dwProcessId))


__all__ = ["TokenProcess", "spawn_with_token"]
