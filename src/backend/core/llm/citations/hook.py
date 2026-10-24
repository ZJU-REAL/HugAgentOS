"""Annotate a buffered neutral tool result before model and SSE exposure."""
from collections.abc import Mapping
from core.harness.events import thaw_value
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage
from core.llm.citations.allocator import CITATION_ALLOCATOR
from core.llm.citations.anchor import annotate_tool_result
from core.llm.citations.anchor_state import SKIP_TOOLS


def citation_candidate(name):
    return bool(name and name not in SKIP_TOOLS)


def annotate_result(invocation):
    if not citation_candidate(invocation.operation_name):
        return Decision()
    result = thaw_value(invocation.data.get("tool_result"))
    if not result or result.get("state") != "success":
        return Decision()
    blocks = result.get("content") or []
    if not blocks or any(not isinstance(block, Mapping) or block.get("type") != "text" for block in blocks):
        return Decision()
    allocator = CITATION_ALLOCATOR.get()
    if allocator is None:
        return Decision()
    tool_id = str(invocation.data.get("metadata", {}).get("tool_call_id") or "")
    text = "".join(block.get("text") or "" for block in blocks)
    annotated, items = annotate_tool_result(invocation.operation_name, tool_id, text, allocator)
    if not items:
        return Decision()
    allocator.register(tool_id, items)
    result["content"] = [{"type": "text", "text": annotated}]
    return Decision.modify({"tool_result": result})


def citation_hook():
    return HookSpec("citation_anchor", HookStage.AFTER_TOOL, annotate_result, priority=200,
                    failure_mode=FailureMode.OPEN, mutable_fields=frozenset({"tool_result"}))
