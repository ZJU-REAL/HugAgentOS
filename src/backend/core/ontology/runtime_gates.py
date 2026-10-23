"""Deterministic ontology checks on tool calls and outputs."""
from __future__ import annotations
from typing import Any, Iterable
from jsonschema import Draft202012Validator, ValidationError
from core.ontology import validation_models

def evaluate_tool_call(
    runtime: dict[str, Any],
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    completed_tools: Iterable[str] = (),
) -> validation_models.OntologyGateDecision:
    """Apply zero-LLM L-a validation to one tool call."""
    if not runtime.get("enabled"):
        return validation_models.OntologyGateDecision(True, "pass")
    completed = set(completed_tools)
    violations: list[dict[str, Any]] = []
    suggestions: list[str] = []
    matched: list[str] = []
    enforced = False

    for pack in runtime.get("packs", []):
        for workflow in pack.get("workflows", []):
            if tool_name in workflow.get("forbidden_tools", []):
                violations.append(
                    {
                        "rule_id": f"workflow:{workflow['id']}:forbidden",
                        "message": f"工作流 {workflow['name']} 禁止调用工具 {tool_name}",
                        "mode": "enforce",
                    }
                )
                enforced = True
        for rule in pack.get("constraints", []):
            target = rule.get("target") or {}
            if target.get("tool") != tool_name:
                continue
            matched.append(rule["id"])
            missing_prerequisites = [
                name for name in rule.get("prerequisite_tools", []) if name not in completed
            ]
            rule_errors: list[str] = []
            if missing_prerequisites:
                rule_errors.append("缺少前置工具调用: " + ", ".join(missing_prerequisites))
            try:
                if target.get("kind") == "tool":
                    Draft202012Validator(rule.get("schema") or {}).validate(tool_input)
                elif target.get("kind") == "tool_parameter":
                    parameter = target.get("parameter")
                    Draft202012Validator(rule.get("schema") or {}).validate(
                        tool_input.get(parameter)
                    )
            except ValidationError as exc:
                rule_errors.append(exc.message)
            if rule_errors:
                violations.append(
                    {
                        "pack_id": pack.get("pack_id"),
                        "rule_id": rule["id"],
                        "message": rule["message"],
                        "reasons": rule_errors,
                        "mode": rule.get("mode", "log"),
                        "risk": rule.get("risk", "low"),
                    }
                )
                if rule.get("suggestion"):
                    suggestions.append(rule["suggestion"])
                if rule.get("mode") == "enforce":
                    enforced = True

    if not violations:
        return validation_models.OntologyGateDecision(True, "pass", matched_rule_ids=matched)
    return validation_models.OntologyGateDecision(
        allowed=not enforced,
        decision="deny" if enforced else "log",
        violations=violations,
        suggestions=suggestions,
        matched_rule_ids=matched,
    )


def evaluate_output(
    runtime: dict[str, Any],
    *,
    answer: str,
    citations: Iterable[dict[str, Any]] = (),
    completed_tools: Iterable[str] | None = (),
) -> validation_models.OntologyGateDecision:
    """Apply deterministic output constraints before any LLM reviewer."""
    if not runtime.get("enabled"):
        return validation_models.OntologyGateDecision(True, "pass")
    citation_items = list(citations)
    # ``None`` means the caller cannot observe a complete tool trace (the
    # legacy non-streaming reply path).  Output and citation rules still run,
    # but required-tool rules must not produce a false omission claim.
    completed = set(completed_tools) if completed_tools is not None else None
    violations: list[dict[str, Any]] = []
    matched: list[str] = []
    suggestions: list[str] = []
    enforced = False
    for pack in runtime.get("packs", []):
        if completed is not None:
            for workflow in pack.get("workflows", []):
                missing_tools = sorted(set(workflow.get("required_tools", [])) - completed)
                if missing_tools:
                    violations.append(
                        {
                            "pack_id": pack.get("pack_id"),
                            "rule_id": f"workflow:{workflow.get('id')}:required",
                            "message": "领域工作流缺少必需的证据工具调用。",
                            "reasons": ["未调用: " + ", ".join(missing_tools)],
                            "mode": "enforce",
                            "risk": workflow.get("risk", "low"),
                        }
                    )
                    enforced = True
        output_tags = {
            tag for workflow in pack.get("workflows", []) for tag in workflow.get("output_tags", [])
        }
        for rule in pack.get("constraints", []):
            target = rule.get("target") or {}
            if target.get("kind") != "output":
                continue
            if output_tags and target.get("output_tag") not in output_tags:
                continue
            matched.append(rule["id"])
            reasons: list[str] = []
            if rule.get("requires_citations") and not citation_items:
                reasons.append("输出缺少可追溯引用证据")
            try:
                Draft202012Validator(rule.get("schema") or {}).validate(answer)
            except ValidationError as exc:
                reasons.append(exc.message)
            if reasons:
                violations.append(
                    {
                        "pack_id": pack.get("pack_id"),
                        "rule_id": rule["id"],
                        "message": rule["message"],
                        "reasons": reasons,
                        "mode": rule.get("mode", "log"),
                        "risk": rule.get("risk", "low"),
                    }
                )
                if rule.get("suggestion"):
                    suggestions.append(rule["suggestion"])
                if rule.get("mode") == "enforce":
                    enforced = True
    if not violations:
        return validation_models.OntologyGateDecision(True, "pass", matched_rule_ids=matched)
    return validation_models.OntologyGateDecision(
        allowed=not enforced,
        decision="deny" if enforced else "log",
        violations=violations,
        suggestions=suggestions,
        matched_rule_ids=matched,
    )
