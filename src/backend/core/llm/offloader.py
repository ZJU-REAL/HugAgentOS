# -*- coding: utf-8 -*-
"""Sandbox Offloader — on-disk implementation of the AgentScope 2.0 `Offloader` protocol.

Background: when the 2.0 ``Agent`` compresses context / truncates overlong tool
results, if an ``offloader`` was passed at construction time it hands the
truncated "overflow portion" to it for persistence, and splices the returned
path into the ``<system-reminder>`` given to the model ("You can refer to the
file in '{path}'"). Without an offloader, that content is **simply discarded**.

This implementation writes the overflow into the **sandbox** under
``/workspace/.offload/``. Because the agent's ``Read`` and ``bash`` tools go
through the same sandbox session (the ``_sess`` resolved by
``resolve_sandbox_session``), the model can read the full content back via
``Read("/workspace/.offload/xxx.txt")`` or ``bash(cat/grep …)``, turning
"silent truncation" into "look it up on demand".

Constraint: per the protocol contract — as long as the agent has an offloader
attached, hitting truncation **always** calls into here and stuffs the return
value into the prompt, so these methods **must never raise** (otherwise the
whole reply turn crashes). All write failures are swallowed internally and a
one-line degradation note is returned. Only mounted when sandbox tools are
enabled (otherwise the agent has no Read/bash and persistence is pointless).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from core.llm.message_compat import flatten_tool_output

logger = logging.getLogger(__name__)

# Persistence directory inside the sandbox. Hidden directory (dot prefix) to
# avoid polluting the "My Space" pin stream / accidental display to users.
# put_file auto-creates parent directories. The workspace root reads from a
# single source (the no-Docker local profile follows SCRIPT_RUNNER_WORKSPACE;
# the Docker profile default /workspace is unchanged).
from core.sandbox._common import WORKSPACE as _WS

OFFLOAD_DIR = f"{_WS}/.offload"


class SandboxOffloader:
    """Persist tool results / historical context that overflow during compression to the sandbox ``/workspace/.offload/``.

    Args:
        provider: A sandbox provider implementing the ``SandboxProvider``
            protocol (including ``put_file``).
        sandbox_session_id: The sandbox session identifier shared with the
            agent's bash/Read tools (result of ``resolve_sandbox_session(...)``;
            may be a chat_id, ``""`` ephemeral, or ``None``).
    """

    def __init__(self, provider: Any, sandbox_session_id: Optional[str]) -> None:
        self._provider = provider
        self._sess = sandbox_session_id

    async def _write(self, path: str, text: str) -> str:
        """Write a file into the sandbox and return its path; on failure return a degradation note (never raises)."""
        try:
            await self._provider.put_file(self._sess, path, text.encode("utf-8"))
            logger.info("[offloader] wrote %d chars → %s", len(text), path)
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("[offloader] put_file 失败 path=%s: %s", path, exc)
            return "（完整内容落盘失败，暂不可读）"

    async def offload_tool_result(self, session_id: str, tool_result: Any) -> str:
        """Persist the truncated overflow portion of a tool result; return the sandbox path."""
        text = flatten_tool_output(getattr(tool_result, "output", None))
        tid = getattr(tool_result, "id", None) or uuid.uuid4().hex[:8]
        # tool_call ids look like call_xxx / uuid, filename-safe; still scrub separators as a safety net.
        safe_tid = str(tid).replace("/", "_").replace("..", "_")
        return await self._write(f"{OFFLOAD_DIR}/tool_{safe_tid}.txt", text)

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
                        f"[{role}/tool_call {getattr(b, 'name', '')}] "
                        f"{getattr(b, 'input', '')}"
                    )
                elif btype == "tool_result":
                    parts.append(
                        f"[{role}/tool_result] "
                        f"{flatten_tool_output(getattr(b, 'output', None))}"
                    )
        text = "\n\n".join(parts)
        return await self._write(
            f"{OFFLOAD_DIR}/context_{uuid.uuid4().hex[:8]}.txt", text
        )
