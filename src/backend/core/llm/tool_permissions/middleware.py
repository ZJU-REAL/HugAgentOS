"""One permission dispatch boundary: live policy, authorization and ticket lifetime."""

from __future__ import annotations
import json
from typing import Any, Mapping
from agentscope.agent import Agent
from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolResponse
from .models import CURRENT_PERMISSION_TICKET, PermissionEnforcementError
from .service import ToolPermissionService
from .preferences import refresh_runtime


def _permission_response(
    tool_call: Any,
    payload: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> ToolResponse:
    success = bool(payload.get("ok")) and not payload.get("error")
    return ToolResponse(
        id=str(getattr(tool_call, "id", "") or ""),
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, default=str),
            )
        ],
        state=ToolResultState.SUCCESS if success else ToolResultState.ERROR,
        metadata={"permission": dict(audit)},
    )


class ToolPermissionMiddleware(MiddlewareBase):
    """Authorize once before effects and bind the ticket around dispatch."""

    def __init__(self, service: ToolPermissionService) -> None:
        self.service = service

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        async for item in self._act(input_kwargs, next_handler):
            yield item

    async def _act(self, input_kwargs: dict, next_handler):  # noqa: ANN001
        tool_call = input_kwargs.get("tool_call")
        try:
            runtime = await refresh_runtime(self.service.runtime)
        except PermissionEnforcementError as exc:
            yield _permission_response(
                tool_call,
                {"error": str(exc), "blocked": True},
                {"decision": "deny", "reason": "policy_unavailable"},
            )
            return
        service = ToolPermissionService(self.service.registry, runtime)
        outcome = await service.authorize(tool_call)
        if not outcome.proceed:
            yield _permission_response(
                tool_call,
                outcome.payload or {"error": "工具权限判定拒绝执行", "blocked": True},
                outcome.audit,
            )
            return

        if outcome.ticket is None:
            async for item in next_handler(**input_kwargs):
                if isinstance(item, ToolResponse):
                    item.metadata = {
                        **dict(item.metadata or {}),
                        "permission": dict(outcome.audit),
                    }
                yield item
            return

        token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
        try:
            async for item in next_handler(**input_kwargs):
                if isinstance(item, ToolResponse):
                    item.metadata = {
                        **dict(item.metadata or {}),
                        "permission": outcome.ticket.audit_dict(),
                    }
                yield item
        finally:
            outcome.ticket.lifetime.expire()
            CURRENT_PERMISSION_TICKET.reset(token)
