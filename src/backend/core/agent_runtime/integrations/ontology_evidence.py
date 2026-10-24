"""Collect and summarise ontology governance evidence for the final answer review."""

from __future__ import annotations
from core.ontology import output_review_state
from core.llm.citations.allocator import collect_citation_dicts

import json
import logging
from typing import Any, Dict, List, Optional

from core.llm.citations.allocator import AnchorAllocator

logger = logging.getLogger(__name__)


def _ontology_review_event_context(runtime: Dict[str, Any]) -> Dict[str, Any]:
    committee_size = max(
        (int(pack.get("config", {}).get("committee_size", 3)) for pack in runtime.get("packs", [])),
        default=3,
    )
    workflows = [
        {
            "pack_id": pack.get("pack_id"),
            "workflow_id": workflow.get("id"),
            "workflow_name": workflow.get("name"),
        }
        for pack in runtime.get("packs", [])
        for workflow in pack.get("workflows", [])
    ]
    review_state = runtime.get("output_review") or {}
    return {
        "governance_run_id": runtime.get("governance_run_id"),
        "review_owner": review_state.get("owner"),
        "review_count": int(review_state.get("count") or 0),
        "committee_size": committee_size,
        "workflows": workflows,
        "activation_sources": sorted(
            {
                str(event.get("source"))
                for event in runtime.get("runtime_events", [])
                if event.get("type") == "ontology_activation" and event.get("source")
            }
        ),
    }


def _ontology_governance_summary(runtime: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build the compact, persisted governance card payload for one answer."""
    if not runtime.get("enabled"):
        return None
    events = list(runtime.get("runtime_events") or [])
    activations = [
        {
            key: event.get(key)
            for key in (
                "pack_id",
                "workflow_id",
                "workflow_name",
                "source",
                "asset_kind",
                "asset_id",
                "review_level",
            )
            if event.get(key) is not None
        }
        for event in events
        if event.get("type") == "ontology_activation"
    ]
    gates = [
        {
            key: event.get(key)
            for key in (
                "decision",
                "tool_name",
                "matched_rule_ids",
                "violations",
                "denial_count",
                "circuit_breaker",
            )
            if event.get(key) is not None
        }
        for event in events
        if event.get("type") == "ontology_gate"
    ]
    review_state = dict(runtime.get("output_review") or {})
    review_state["level"] = runtime.get("review_level", "none")
    review_state["committee_size"] = _ontology_review_event_context(runtime)["committee_size"]
    if not activations and not gates and review_state.get("status") == "pending":
        return None
    if review_state.get("status") == "pending" and not output_review_state.requires_output_review(runtime):
        review_state["status"] = "skipped"
    return {
        "governance_run_id": runtime.get("governance_run_id"),
        "activations": activations,
        "gates": gates,
        "review": review_state,
    }


def _ontology_review_owner(runtime: Dict[str, Any], context: Dict[str, Any]) -> str:
    """Stable owner token for the one outer-workflow final review."""
    run_id = runtime.get("governance_run_id") or context.get("chat_id") or "request"
    return f"outer_workflow:{run_id}"


def _ontology_review_failure_result(answer: str, exc: Exception) -> Dict[str, Any]:
    """Keep a completed draft usable when the post-answer review itself fails."""
    logger.error(
        "[ontology-review] automatic review failed; preserving original answer: %s",
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return {
        "verdict": "escalate",
        "answer": answer,
        "revised": False,
        "annotated": True,
        "repair_attempts": 0,
        "attempts": 1,
        "violations": [],
        "affected_claims": [],
        "evidence": [],
        "feedback": ["自动评审未正常完成，已保留原文并转人工复核。"],
        "manual_review": {
            "required": True,
            "title": "领域本体人工复核",
            "summary": "自动评审未正常完成，原文已保留，请人工核对或重新发起评审。",
            "items": [
                {
                    "quote": "本轮完整答案",
                    "rule_id": "ontology_review_runtime",
                    "risk": "自动评审未完成，系统无法确认答案满足全部领域约束。",
                    "manual_check": "核对关键事实、推断、适用前提和原始工具证据。",
                }
            ],
            "actions": ["重新发起本体评审，或在采用结论前完成人工核对。"],
        },
        "new_tools": [],
        "new_citation_count": 0,
        "latency_ms": None,
    }


def _record_ontology_activations(
    activations: List[Dict[str, Any]],
    runtime: Dict[str, Any],
    context: Dict[str, Any],
) -> None:
    if not activations:
        return
    try:
        from core.ontology.audit_events import record_runtime_activation

        for event in activations:
            record_runtime_activation(
                event,
                runtime,
                user_id=str(context.get("user_id", "")) or None,
                chat_id=context.get("chat_id"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ontology] workflow activation audit persistence failed: %s", exc)


def _bounded_trace_value(value: Any, max_chars: int = 4000) -> Any:
    """Keep reviewer evidence useful without copying unbounded tool output."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= max_chars:
        return value
    return {"truncated": True, "preview": text[:max_chars]}


def _parse_tool_result_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value) if value else {}
    except json.JSONDecodeError:
        return {"result": value}


def _capture_nested_ontology_evidence(
    payload: Dict[str, Any],
    trace: List[Dict[str, Any]],
    citations: List[Dict[str, Any]],
    allocator: Optional[AnchorAllocator] = None,
) -> List[Dict[str, Any]]:
    """Merge trusted ``call_subagent`` bypass events into the outer review trace."""
    sub_type = str(payload.get("sub_type") or "")
    tool_name = str(payload.get("tool_name") or "")
    tool_id = str(payload.get("tool_id") or "")
    provenance = {
        key: payload.get(key)
        for key in ("agent_id", "agent_name", "sub_run_id", "parent_tool_id")
        if payload.get(key) is not None
    }
    if sub_type == "tool_call" and tool_name and payload.get("input") is not None:
        trace.append(
            {
                "type": "tool_call",
                "tool_id": tool_id,
                "tool_name": tool_name,
                "input": _bounded_trace_value(payload.get("input")),
                "source": "subagent",
                **provenance,
            }
        )
        return []
    if sub_type != "tool_result" or not tool_name or payload.get("status") == "error":
        return []

    result = _parse_tool_result_value(payload.get("output"))
    cit_dicts = collect_citation_dicts(tool_id, allocator)
    citations.extend(cit_dicts)
    trace.append(
        {
            "type": "tool_result",
            "tool_id": tool_id,
            "tool_name": tool_name,
            "result": _bounded_trace_value(result),
            "citations": cit_dicts,
            "source": "subagent",
            **provenance,
        }
    )
    return cit_dicts
