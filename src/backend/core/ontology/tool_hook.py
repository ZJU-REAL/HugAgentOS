"""Ontology tool admission and audit through neutral execution hooks."""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from core.harness.context_items import reminder_item
from core.harness.events import thaw_value
from core.harness.hooks import Decision, DecisionKind, HookSpec, HookStage

logger = logging.getLogger(__name__)


class OntologyToolHook:
    def __init__(self, runtime=None):
        self.runtime = dict(runtime or {})
        self.completed_tools = set()
        self.denial_counts = {}

    async def __call__(self, invocation):
        if invocation.stage == HookStage.AFTER_TOOL:
            result = invocation.data.get("tool_result") or {}
            if result.get("state") not in {"error", "denied", "interrupted"}:
                self.completed_tools.add(invocation.operation_name)
            return Decision()
        from core.ontology.runtime_pack import activate_runtime_for_asset, render_runtime_prompt
        from core.ontology.runtime_gates import evaluate_tool_call
        from core.ontology.runtime_policy import resolve_runtime_asset_tags
        runtime = thaw_value(invocation.data.get("ontology_runtime") or self.runtime)
        if not runtime.get("enabled"):
            return Decision()
        identity = invocation.data.get("identity", {})
        started = time.monotonic()
        tool_name = invocation.operation_name
        tool_input = thaw_value(invocation.data.get("tool_args") or {})
        asset_kind, asset_id = self._resolve_invoked_asset(tool_name, tool_input)
        await asyncio.to_thread(resolve_runtime_asset_tags, runtime=runtime, kind=asset_kind,
                                asset_id=asset_id, user_id=str(identity.get("user_id") or ""))
        activations = activate_runtime_for_asset(runtime, kind=asset_kind, asset_id=asset_id)
        items = list(invocation.data.get("context_items") or ())
        if activations:
            contract = render_runtime_prompt(runtime)
            if contract:
                items.append(reminder_item(
                    "检测到受领域本体治理的运行时资产，策略已升级且本轮不可降级。\n" + contract,
                    "harness:ontology_gate"))
            await self._audit_activations(identity, runtime, activations)
        decision = evaluate_tool_call(runtime, tool_name=tool_name, tool_input=tool_input,
                                      completed_tools=self.completed_tools)
        patch = {"ontology_runtime": runtime, "context_items": items}
        if decision.allowed:
            await self._audit(identity, runtime, tool_name, decision, started)
            return Decision.modify(patch)
        key = ",".join(decision.matched_rule_ids) or tool_name
        count = self.denial_counts[key] = self.denial_counts.get(key, 0) + 1
        configs = [pack.get("config", {}) for pack in runtime.get("packs", [])]
        strategy = min((int(item.get("repeated_denial_threshold", 2)) for item in configs), default=2)
        breaker = min((int(item.get("circuit_breaker_threshold", 5)) for item in configs), default=5)
        guidance = list(decision.suggestions)
        if count >= strategy:
            guidance.append("同一规则已重复拦截，请改变执行策略，不要原样重试。")
        if count >= breaker:
            guidance.append("本体门禁已触发熔断；停止调用该工具，并向用户说明缺失条件。")
        await self._audit(identity, runtime, tool_name, decision, started,
                          denial_count=count, circuit_breaker=count >= breaker)
        if count == strategy and identity.get("chat_id"):
            try:
                from core.ontology.evolution import schedule_ontology_evolution
                schedule_ontology_evolution(user_id=str(identity.get("user_id") or "system"))
            except Exception as exc:
                logger.warning("[ontology] evolution scheduling failed: %s", exc)
        payload = {"code": "ONTOLOGY_GATE_DENIED", "tool": tool_name,
                   "violations": decision.violations, "suggestions": guidance,
                   "denial_count": count, "circuit_breaker": count >= breaker}
        patch["tool_response"] = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                                  "state": "denied", "metadata": {"ontology_gate": payload}}
        return Decision(DecisionKind.REJECT, patch=patch, reason="ontology gate denied tool")

    async def _audit(
        self,
        identity: dict,
        runtime: dict[str, Any],
        tool_name: str,
        decision,
        started: float,
        *,
        denial_count: int = 0,
        circuit_breaker: bool = False,
    ) -> None:  # noqa: ANN001
        if not decision.violations and not decision.matched_rule_ids:
            return
        runtime.setdefault("runtime_events", []).append(
            {
                "type": "ontology_gate",
                "status": "completed",
                "governance_run_id": runtime.get("governance_run_id"),
                "stage": "tool",
                "decision": decision.decision,
                "tool_name": tool_name,
                "matched_rule_ids": list(decision.matched_rule_ids),
                "violations": list(decision.violations),
                "suggestions": list(decision.suggestions),
                "denial_count": denial_count,
                "circuit_breaker": circuit_breaker,
            }
        )
        try:
            from core.ontology.audit_events import record_enforcement_event

            first = decision.violations[0] if decision.violations else {}
            pack_id = first.get("pack_id")
            version_id = None
            for pack in runtime.get("packs", []):
                if pack.get("pack_id") == pack_id:
                    version_id = pack.get("version_id")
                    break
            payload = {
                "user_id": identity.get("user_id"),
                "chat_id": identity.get("chat_id"),
                "pack_id": pack_id,
                "version_id": version_id,
                "rule_id": first.get("rule_id")
                or (decision.matched_rule_ids[0] if decision.matched_rule_ids else None),
                "stage": "tool",
                "event_type": "tool_call_gate",
                "decision": decision.decision,
                "mode": first.get("mode", "enforce"),
                "target": tool_name,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "details": {
                    "governance_run_id": runtime.get("governance_run_id"),
                    "violations": decision.violations,
                    "matched_rule_ids": decision.matched_rule_ids,
                    "suggestions": decision.suggestions,
                    "denial_count": denial_count,
                    "circuit_breaker": circuit_breaker,
                },
            }
            await asyncio.to_thread(record_enforcement_event, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ontology] audit event persistence failed: %s", exc)


    @staticmethod
    def _resolve_invoked_asset(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
        if tool_name == "call_subagent" and tool_input.get("agent_id"):
            return "subagent", str(tool_input["agent_id"])
        if tool_name == "view_text_file":
            file_path = str(tool_input.get("file_path") or "")
            path = Path(file_path)
            if path.name == "SKILL.md" and path.parent.name:
                return "skill", path.parent.name
        return "tool", tool_name


    @staticmethod
    async def _audit_activations(
        identity: dict,
        runtime: dict[str, Any],
        activations: list[dict[str, Any]],
    ) -> None:
        try:
            from core.ontology.audit_events import record_runtime_activation

            for event in activations:
                await asyncio.to_thread(
                    record_runtime_activation,
                    event,
                    runtime,
                    user_id=identity.get("user_id"),
                    chat_id=identity.get("chat_id"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ontology] activation audit persistence failed: %s", exc)



def ontology_tool_hooks(runtime):
    policy = OntologyToolHook(runtime)
    return (HookSpec("ontology_admission", HookStage.BEFORE_TOOL, policy, priority=500,
                     timeout_ms=30000, mutable_fields=frozenset({"ontology_runtime", "context_items", "tool_response"})),
            HookSpec("ontology_completion", HookStage.AFTER_TOOL, policy, priority=300))
