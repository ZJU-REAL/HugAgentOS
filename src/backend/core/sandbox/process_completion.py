"""Completion client for internal jobs using the managed process protocol.

Unlike model tools, internal jobs need a final result. They consume successive
increments from the same process; a transport failure never starts another one.
"""

from __future__ import annotations

import asyncio
from .errors import SandboxError
from .protocol import ProcessRequest, ProcessResult

MAX_CAPTURE_CHARS = 1024 * 1024


class CompletionMixin:
    """Final-result API shared by sandbox providers for internal business jobs."""

    async def run_to_completion(self, request: ProcessRequest) -> ProcessResult:
        """Await one managed process, with bounded capture and explicit truncation."""
        start = asyncio.create_task(self.start_process(request, yield_time_ms=10000))
        try:
            result = await asyncio.shield(start)
        except asyncio.CancelledError:
            # A cancelled waiter does not cancel the launch. Harvest its handle
            # and interrupt that process only, without retrying or closing the chat.
            async def stop_after_start():
                try:
                    launched = await start
                    if launched.get("session_id"):
                        await self.write_stdin(
                            launched["session_id"],
                            sandbox_session_id=request.session_id,
                            user_id=request.user_id,
                            chars="\x03",
                            yield_time_ms=1000,
                        )
                except Exception:
                    pass

            cleanup = asyncio.create_task(stop_after_start())
            await asyncio.shield(cleanup)
            raise
        stdout = stderr = ""
        omitted = 0
        session_id = result.get("session_id")
        try:
            while True:
                for stream in ("stdout", "stderr"):
                    value = (stdout if stream == "stdout" else stderr) + (result.get(stream) or "")
                    omitted += max(0, len(value) - MAX_CAPTURE_CHARS)
                    if stream == "stdout":
                        stdout = value[-MAX_CAPTURE_CHARS:]
                    else:
                        stderr = value[-MAX_CAPTURE_CHARS:]
                omitted += result.get("output_omitted_chars", 0)
                if result.get("error"):
                    raise SandboxError(result["error"])
                if result.get("status") == "exited":
                    if result.get("exit_code") is None:
                        raise SandboxError("Process ended without an exit code")
                    if omitted:
                        raise SandboxError(
                            f"Process output exceeded capture limit ({omitted} characters omitted)"
                        )
                    return ProcessResult(
                        stdout, stderr, result["exit_code"], result.get("execution_time_ms", 0)
                    )
                session_id = result.get("session_id")
                if result.get("status") != "running" or not session_id:
                    raise SandboxError("Invalid managed process response")
                result = await self.write_stdin(
                    session_id,
                    sandbox_session_id=request.session_id,
                    user_id=request.user_id,
                    yield_time_ms=60000,
                )
        except BaseException:
            if session_id:
                try:
                    await asyncio.shield(
                        self.write_stdin(
                            session_id,
                            sandbox_session_id=request.session_id,
                            user_id=request.user_id,
                            chars="\x03",
                            yield_time_ms=1000,
                        )
                    )
                except Exception:
                    pass
            raise
