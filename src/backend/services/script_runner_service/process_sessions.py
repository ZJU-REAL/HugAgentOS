"""Managed command lifetimes; independent of any HTTP wait or model turn.

Standalone so the runner image can import it without the backend package.
Handles implement poll(), interrupt(), close(), and optionally log_paths.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

MAX_PROCESSES = 64
MAX_PENDING_CHARS = 262144
COMPLETED_RETENTION_S = 600


class ProcessSessionError(ValueError):
    pass


@dataclass
class Entry:
    owner: tuple[str, str]
    handle: Any = None
    closing: bool = False
    cleaning: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    start_error: BaseException | None = None
    log_paths: dict | None = None
    metadata: dict = field(default_factory=dict)
    task: asyncio.Task | None = None
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stdout: str = ""
    stderr: str = ""
    omitted_chars: int = 0
    exit_code: int | None = None
    error: str | None = None

    def append(self, stream: str, text: str) -> None:
        pending = getattr(self, stream) + text
        dropped = max(0, len(pending) - MAX_PENDING_CHARS)
        self.omitted_chars += dropped
        setattr(self, stream, pending[-MAX_PENDING_CHARS:])


class ProcessSessions:
    def __init__(self):
        self.entries: dict[str, Entry] = {}
        self.closing_owners: dict[str, asyncio.Task] = {}

    async def start(
        self,
        factory: Callable[[], Awaitable[Any]],
        owner: tuple[str, str],
        yield_time_ms: int = 10000,
        timeout: int | None = None,
    ) -> dict:
        if owner[0] in self.closing_owners:
            raise ProcessSessionError("Conversation is closing")
        if not owner[0]:
            raise ProcessSessionError("A conversation sandbox session is required")
        if timeout is not None and timeout <= 0:
            raise ProcessSessionError("timeout must be positive or omitted")
        now = time.monotonic()
        for sid, old in list(self.entries.items()):
            if old.finished is not None and now - old.finished > COMPLETED_RETENTION_S:
                del self.entries[sid]
        # Reclaim completed entries only; never silently kill a running job.
        while len(self.entries) >= MAX_PROCESSES:
            completed = next((sid for sid, e in self.entries.items() if e.done.is_set()), None)
            if completed is None:
                raise ProcessSessionError(
                    "Too many running commands; wait or interrupt an existing session"
                )
            del self.entries[completed]
        sid = uuid.uuid4().hex
        entry = Entry(owner)
        self.entries[sid] = entry  # reserve before the first await
        entry.task = asyncio.create_task(self._launch(entry, factory, timeout))
        await entry.ready.wait()
        if entry.start_error:
            raise ProcessSessionError(f"Command could not start: {entry.start_error}")
        # Cancellation of this wait must not cancel a successfully started process.
        return await self.write(sid, owner, "", yield_time_ms)

    async def _launch(self, entry, factory, timeout):
        try:
            entry.handle = await factory()
            if entry.closing:
                entry.cleaning = True
                await entry.handle.close()
                raise ProcessSessionError("Conversation closed during command startup")
            entry.log_paths = getattr(entry.handle, "log_paths", None)
            entry.metadata = getattr(entry.handle, "metadata", {})
        except BaseException as exc:
            entry.start_error = exc
            entry.error = (
                "Command startup cancelled" if isinstance(exc, asyncio.CancelledError) else str(exc)
            )
            entry.exit_code = -1
            entry.finished = time.monotonic()
            entry.done.set()
            return
        finally:
            entry.ready.set()
        await self._monitor(entry, timeout)

    async def _monitor(self, entry: Entry, timeout: int | None) -> None:
        try:
            while True:
                remaining = (
                    None if timeout is None else timeout - (time.monotonic() - entry.started)
                )
                if remaining is not None and remaining <= 0:
                    entry.error = f"Command execution deadline exceeded ({timeout}s)"
                    entry.exit_code = 124
                    break
                try:
                    out, err, code = await asyncio.wait_for(
                        entry.handle.poll(),
                        timeout=min(30, remaining) if remaining is not None else 30,
                    )
                except asyncio.TimeoutError:
                    if remaining is not None and remaining <= 30:
                        entry.error = f"Command execution deadline exceeded ({timeout}s)"
                        entry.exit_code = 124
                        break
                    raise ProcessSessionError(
                        "Process status request timed out; execution state is uncertain"
                    )
                entry.append("stdout", out)
                entry.append("stderr", err)
                if code is not None:
                    entry.exit_code = code
                    break
                if timeout is not None and time.monotonic() - entry.started >= timeout:
                    entry.error = f"Command execution deadline exceeded ({timeout}s)"
                    entry.exit_code = 124
                    break
                await asyncio.sleep(getattr(entry.handle, "poll_interval", 0.1))
        except asyncio.CancelledError:
            entry.error = "Command session closed"
            entry.exit_code = -1
        except Exception as exc:
            entry.error = str(exc)
            entry.exit_code = -1
        finally:
            entry.cleaning = True
            cleanup = asyncio.create_task(asyncio.wait_for(entry.handle.close(), timeout=15))
            try:
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Closing a conversation must not interrupt an already
                    # running cleanup (nor leak a detached cleanup task).
                    await asyncio.shield(cleanup)
            except Exception as exc:
                entry.error = entry.error or f"Process cleanup failed: {exc}"
            entry.handle = None
            entry.finished = time.monotonic()
            entry.done.set()

    async def write(
        self,
        session_id: str,
        owner: tuple[str, str],
        chars: str = "",
        yield_time_ms: int = 60000,
    ) -> dict:
        entry = self.entries.get(session_id)
        if entry is None or entry.owner != owner:
            raise ProcessSessionError("Unknown process session for this user and conversation")
        if chars not in ("", "\x03"):
            raise ProcessSessionError(
                "stdin is closed (non-PTY execution); use empty chars to wait or Ctrl+C to interrupt"
            )
        if chars and not entry.done.is_set() and entry.handle is not None:
            await entry.handle.interrupt()
        # Ctrl+C is delivered before taking this lock, so it can interrupt an
        # existing long poll. Readers serialize to consume each output chunk once.
        async with entry.lock:
            if not entry.done.is_set():
                seconds = max(0, min(int(yield_time_ms), 300000)) / 1000
                try:
                    await asyncio.wait_for(entry.done.wait(), seconds)
                except asyncio.TimeoutError:
                    pass
            result = {
                "session_id": None if entry.done.is_set() else session_id,
                "status": "exited" if entry.done.is_set() else "running",
                "stdout": entry.stdout,
                "stderr": entry.stderr,
                "exit_code": entry.exit_code,
                "execution_time_ms": int(
                    ((entry.finished or time.monotonic()) - entry.started) * 1000
                ),
                "stdin_supported": False,
            }
            if entry.error:
                result["error"] = entry.error
            if entry.omitted_chars:
                result["output_omitted_chars"] = entry.omitted_chars
            result.update(entry.metadata)
            paths = entry.log_paths
            if paths:
                result["output_files"] = paths
            entry.stdout = entry.stderr = ""
            entry.omitted_chars = 0
            return result

    async def close_owner(self, sandbox_session_id: str) -> None:
        task = self.closing_owners.get(sandbox_session_id)
        if task is None:
            task = asyncio.create_task(self._close_owner(sandbox_session_id))
            self.closing_owners[sandbox_session_id] = task
            task.add_done_callback(lambda _: self.closing_owners.pop(sandbox_session_id, None))
        await asyncio.shield(task)

    async def _close_owner(self, sandbox_session_id: str) -> None:
        entries = [(sid, e) for sid, e in self.entries.items() if e.owner[0] == sandbox_session_id]
        for _, entry in entries:
            entry.closing = True
            if entry.task and not entry.task.done() and not entry.cleaning:
                entry.task.cancel()
        await asyncio.gather(
            *(e.task for _, e in entries if e.task and not e.task.done()), return_exceptions=True
        )
        for sid, entry in entries:
            # A task cancelled before its coroutine starts cannot run finally.
            if not entry.ready.is_set():
                entry.start_error = ProcessSessionError(
                    "Conversation closed during command startup"
                )
                entry.ready.set()
                entry.done.set()
            self.entries.pop(sid, None)

    async def close_all(self) -> None:
        for owner in {e.owner[0] for e in self.entries.values()}:
            await self.close_owner(owner)
