"""MCP tool "human confirmation" wrapper — lets **MCP tools running in the separate mcp
container** also reuse the §13 MySpace write-confirmation "true suspension" gate
(``core.llm.tools._myspace_confirm.gate``).

Background and feasibility
--------------------------
MySpace's Delete/Move are **in-process, self-built tools in the backend**: the tool coroutine
directly ``await``s an in-process ``asyncio.Event`` to suspend, the SSE consumer side drains
``ui_signals`` to show the confirmation bar, and the user's out-of-band
``POST /file-confirm`` wakes the same coroutine to resume in place. The whole state machine
(pending/event/ui_signals) lives in the **backend process**, same process as the SSE consumer
loop, which is why suspend-resume works.

The scheduled-task plugin's (automation_task) tools, however, run in a **separate mcp
container** (streamable-http, port 9108) — its process memory is not shared with the backend,
so a pending registered by calling gate inside the mcp container would never be seen by the
backend SSE loop. But the key fact: **HTTP MCP clients are always created per-request inside
the backend process** (see ``mcp_pool``: HTTP MCP is never pooled even when marked
is_stable). The tool's actual network call (HTTP → mcp container) is issued by this
backend-side client object's ``MCPTool.__call__``.

Hence the approach here: in the **backend process**, wrap the ``__call__`` of automation's
**mutating** tools (create/update/delete) with gate() — suspending the current tool coroutine
**before** the HTTP request is sent, fully reusing MySpace's
``gate/ui_signals/file_confirm/FileConfirmBar/POST /file-confirm`` chain (suspend-in-place,
identical UX to deleting/moving MySpace files). Approved → the HTTP call is sent as-is;
rejected/timed out → the interception result is returned to the model as tool_result and the
network call never happens.

"No intervention inside channels" is implemented at the **caller** (agent_factory): channel
runs / non-interactive modes (batch/sub-agent/plan execution) simply never install this gated
client and use the plain client instead → no confirmation prompt.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

from agentscope.tool import MCPTool
from pydantic import PrivateAttr

from core.llm.mcp_manager import BareNameMCPClient
from core.llm.tools._common import resp_json
from core.llm.tools._myspace_confirm import (
    KIND_AUTOMATION,
    OP_CRON_CREATE,
    OP_CRON_DELETE,
    OP_CRON_UPDATE,
    gate,
)

logger = logging.getLogger(__name__)


# ── Confirmation spec: resolves (op, dedup target, human-readable summary) from tool call args ──
# The dedup target (logical_path slot) is used for gate's (op, target) deduplication — parallel/
# repeated invocations of the **same operation** share one confirmation bar; different operations
# each get their own. The summary is for frontend rendering + interception copy.
ConfirmSpec = Callable[[Dict[str, Any]], Tuple[str, str, str]]


def _clip(text: Any, n: int = 30) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _spec_create(kwargs: Dict[str, Any]) -> Tuple[str, str, str]:
    name = str(kwargs.get("name") or "").strip()
    prompt = str(kwargs.get("prompt") or "").strip()
    cron = str(kwargs.get("cron_expression") or "").strip()
    label = name or _clip(prompt, 30) or "新任务"
    # Dedup target includes cron+label to avoid misjudging "same name, different schedule" as duplicates.
    target = f"create:{label}|{cron}"
    suffix = f"（{cron}）" if cron else ""
    return OP_CRON_CREATE, target, f"创建定时任务「{label}」{suffix}"


def _spec_update(kwargs: Dict[str, Any]) -> Tuple[str, str, str]:
    ref = str(kwargs.get("task_ref") or "").strip() or "该任务"
    return OP_CRON_UPDATE, f"update:{ref}", f"修改定时任务「{_clip(ref, 40)}」"


def _spec_delete(kwargs: Dict[str, Any]) -> Tuple[str, str, str]:
    ref = str(kwargs.get("task_ref") or "").strip() or "该任务"
    return OP_CRON_DELETE, f"delete:{ref}", f"删除定时任务「{_clip(ref, 40)}」"


# Per confirm-required server: "bare tool name → confirmation spec" table. pause/resume are
# reversible and low-risk; by product decision they are not subject to confirmation.
AUTOMATION_CONFIRM_SPECS: Dict[str, ConfirmSpec] = {
    "create_scheduled_task": _spec_create,
    "update_scheduled_task": _spec_update,
    "delete_scheduled_task": _spec_delete,
}

# server key → that server's confirmation spec table (single source of truth)
_SERVER_SPECS: Dict[str, Dict[str, ConfirmSpec]] = {
    "automation_task": AUTOMATION_CONFIRM_SPECS,
}

# Set of MCP servers requiring confirmation, derived from _SERVER_SPECS (avoids a hand-maintained duplicate list).
CONFIRM_MCP_SERVERS = frozenset(_SERVER_SPECS)


def confirm_specs_for(server_key: str) -> Dict[str, ConfirmSpec]:
    return _SERVER_SPECS.get(server_key, {})


class _GatedMCPTool(MCPTool):
    """Subclass that inserts gate() before ``MCPTool.__call__``.

    ``ConfirmGatedMCPClient.get_tool`` activates it by swapping the MCPTool instance's
    ``__class__`` to this class (ToolBase is a pure ABC, not pydantic; the instance memory
    layout is compatible, so the class swap is safe). JSON schema/name/description are all
    inherited from MCPTool unchanged — the tool definition the model sees is identical.
    """

    _confirm_spec: Optional[ConfirmSpec] = None
    _confirm_ctx: Optional[Dict[str, Any]] = None

    async def __call__(self, **kwargs: Any) -> Any:  # type: ignore[override]
        spec = getattr(self, "_confirm_spec", None)
        ctx = getattr(self, "_confirm_ctx", None)
        if spec is None or ctx is None:
            return await super().__call__(**kwargs)
        try:
            op, target, summary = spec(kwargs)
        except Exception:  # noqa: BLE001 — spec resolution failure must not block the tool; let it through
            logger.warning("[mcp-confirm] spec resolve failed for %s", self.name, exc_info=True)
            return await super().__call__(**kwargs)

        # Whether to gate (channel/non-interactive skips) was already decided by the caller when installing the client, hence interactive=True.
        blk = await gate(
            chat_id=ctx.get("chat_id"),
            op=op,
            logical_path=target,
            interactive=True,
            summary=summary,
            kind=KIND_AUTOMATION,
        )
        if blk is not None:
            logger.info("[mcp-confirm] intercepted %s: %s", self.name, blk.get("status"))
            return resp_json(blk)
        return await super().__call__(**kwargs)


class ConfirmGatedMCPClient(BareNameMCPClient):
    """Per-request HTTP MCP client that attaches gate() to mutating tools.

    ``get_tool`` overrides ``BareNameMCPClient`` (which restores tools to their bare names):
    after getting the bare-name MCPTool, if it matches the confirmation spec table, its class
    is swapped to ``_GatedMCPTool`` with the spec + context attached.
    ``list_tools`` goes through the base class → per-item ``get_tool``, so both the listing and
    invocation paths are gated.
    """

    _confirm_ctx: Dict[str, Any] = PrivateAttr(default_factory=dict)
    _confirm_specs: Dict[str, ConfirmSpec] = PrivateAttr(default_factory=dict)

    async def get_tool(self, name: str) -> MCPTool:
        tool = await super().get_tool(name)  # bare name already restored by BareNameMCPClient
        spec = self._confirm_specs.get(tool.name)
        if spec is not None:
            tool.__class__ = _GatedMCPTool  # type: ignore[assignment]
            tool._confirm_spec = spec       # type: ignore[attr-defined]
            tool._confirm_ctx = self._confirm_ctx  # type: ignore[attr-defined]
        return tool


def make_confirm_gated_client(
    name: str,
    cfg: dict,
    *,
    chat_id: Optional[str],
    specs: Dict[str, ConfirmSpec],
) -> ConfirmGatedMCPClient:
    """Construct an HTTP (stateless, per-request) confirmation-gated client.

    Reuses ``mcp_pool.make_client``'s config construction (including the key logic of
    stripping trailing slashes to avoid 307s), only swapping ``client_cls`` to
    ``ConfirmGatedMCPClient`` and attaching the confirmation context/specs.
    """
    from core.llm.mcp_pool import make_client

    client = make_client(name, cfg, is_stateful=False, client_cls=ConfirmGatedMCPClient)
    client._confirm_ctx = {"chat_id": chat_id}  # type: ignore[attr-defined]
    client._confirm_specs = specs               # type: ignore[attr-defined]
    return client
