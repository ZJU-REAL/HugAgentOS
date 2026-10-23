"""Neutral authorization policy with an execution-scoped permission ticket."""
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from core.harness.events import thaw_value
from core.harness.hooks import Decision, DecisionKind, HookSpec, HookStage
from core.sandbox.permissions.dispatch_models import CURRENT_PERMISSION_TICKET, PermissionEnforcementError
from core.sandbox.permissions.approval import refresh_runtime
from .service import ToolPermissionService


@dataclass(frozen=True)
class PermissionCall:
    id: str
    name: str
    input: Any


class ToolPermissionHook:
    def __init__(self, service):
        self.service = service
        self.pending = {}

    async def __call__(self, invocation):
        call = PermissionCall(str(invocation.metadata.get("tool_call_id") or
                                  invocation.data.get("metadata", {}).get("tool_call_id") or ""),
                              invocation.operation_name, thaw_value(invocation.data.get("tool_args", {})))
        try:
            runtime = await refresh_runtime(self.service.runtime)
        except PermissionEnforcementError as exc:
            return self.block(call.id, {"error": str(exc), "blocked": True},
                              {"decision": "deny", "reason": "policy_unavailable"})
        outcome = await ToolPermissionService(self.service.registry, runtime).authorize(call)
        if not outcome.proceed:
            return self.block(call.id, outcome.payload or {"error": "工具权限判定拒绝执行", "blocked": True}, outcome.audit)
        self.pending[call.id] = outcome
        return Decision()

    @staticmethod
    def block(call_id, payload, audit):
        response = {"id": call_id, "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
                    "state": "success" if payload.get("ok") and not payload.get("error") else "error",
                    "metadata": {"permission": dict(audit)}}
        return Decision(DecisionKind.REJECT, patch={"tool_response": response}, reason="permission outcome")

    @contextmanager
    def bind(self, call_id):
        outcome = self.pending.pop(call_id, None)
        if outcome is None:
            yield None
            return
        ticket = outcome.ticket
        if ticket is None:
            yield dict(outcome.audit)
            return
        token = CURRENT_PERMISSION_TICKET.set(ticket)
        try:
            yield ticket.audit_dict()
        finally:
            ticket.lifetime.expire()
            CURRENT_PERMISSION_TICKET.reset(token)

    def discard(self, call_id):
        outcome = self.pending.pop(call_id, None)
        if outcome is not None and outcome.ticket is not None:
            outcome.ticket.lifetime.expire()

    def spec(self):
        return HookSpec("tool_permission", HookStage.BEFORE_TOOL, self, priority=600,
                        timeout_ms=None, mutable_fields=frozenset({"tool_response"}))
