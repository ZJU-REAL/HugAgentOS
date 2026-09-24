# -*- coding: utf-8 -*-
"""Persist truncated tool output in the same workspace as Read and bash.

Only a successful write returns a path. Callers must catch OffloadError and
render an explicit unavailable notice instead of interpolating an error as a
filename. This applies to tool truncation and both context-compression paths.
"""
from __future__ import annotations

import asyncio
import logging
import ntpath
import posixpath
import uuid
from typing import Any, Optional

from core.llm.message_compat import flatten_tool_output

logger = logging.getLogger(__name__)
_WRITE_TIMEOUT_SEC = 30


class OffloadError(RuntimeError):
    """No readable offload file could be published."""


class SandboxOffloader:
    """Persist overflow under the current tool workspace's hidden .offload directory.

    Args:
        provider: A sandbox provider implementing the ``SandboxProvider``
            protocol (including ``put_file``).
        sandbox_session_id: The sandbox session identifier shared with the
            agent's bash/Read tools (result of ``resolve_sandbox_session(...)``;
            must be persistent for later reads).
    """

    def __init__(
        self, provider: Any, sandbox_session_id: Optional[str], user_id: Optional[str] = None
    ) -> None:
        self._provider = provider
        self._sess = sandbox_session_id
        self._user_id = user_id

    def _path(self, prefix: str) -> str:
        from core.llm.tools._paths import workspace_directory

        if not self._sess:
            raise OffloadError("A persistent tool workspace is required")
        root = workspace_directory(self._sess)
        filename = f"{prefix}_{uuid.uuid4().hex}.txt"
        if ntpath.splitdrive(root)[0]:
            return ntpath.join(root, ".offload", filename)
        return posixpath.join(root, ".offload", filename)

    async def _write(self, path: str, text: str) -> str:
        """Publish a path only after the provider confirms the bounded write."""
        try:
            await asyncio.wait_for(
                self._provider.put_file(
                    self._sess, path, text.encode("utf-8"), user_id=self._user_id
                ),
                timeout=_WRITE_TIMEOUT_SEC,
            )
        except Exception as exc:
            # Provider errors may contain transport details; keep them out of
            # model-visible content and log the failure category only.
            logger.warning("[offloader] write failed path=%s kind=%s", path, type(exc).__name__)
            raise OffloadError("Offload file could not be saved") from exc
        logger.info("[offloader] wrote %d chars → %s", len(text), path)
        return path

    async def offload_tool_result(self, session_id: str, tool_result: Any) -> str:
        """Persist the supplied tool text and return its readable workspace path."""
        text = flatten_tool_output(getattr(tool_result, "output", None))
        return await self._write(self._path("tool"), text)

    async def offload_context(self, session_id: str, msgs: Any) -> str:
        """Persist compressed historical messages (flattened into readable text); return the sandbox path."""
        parts: list[str] = []
        for m in msgs or []:
            role = getattr(m, "role", "?")
            for b in getattr(m, "content", None) or []:
                btype = getattr(b, "type", None)
                if btype == "text":
                    parts.append(f"[{role}] {getattr(b, 'text', '')}")
                elif btype == "tool_call":
                    parts.append(
                        f"[{role}/tool_call {getattr(b, 'name', '')}] " f"{getattr(b, 'input', '')}"
                    )
                elif btype == "tool_result":
                    parts.append(
                        f"[{role}/tool_result] "
                        f"{flatten_tool_output(getattr(b, 'output', None))}"
                    )
        text = "\n\n".join(parts)
        return await self._write(self._path("context"), text)
