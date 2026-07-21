"""Tool-call log helpers for streaming SSE event processing.

Pure list/dict utilities that assemble the ``tool_calls_log`` consumed by both
the chat route (``api/routes/v1/chats.py``) and the background run executor
(``routing/chat_run_executor.py``). Relocated here from the chat route so
``routing.*`` no longer imports an API route module (breaks ``routing → api``).
"""

from typing import Any, Dict


def build_thinking_event(chunk: dict, chat_id: str) -> Dict[str, Any]:
    """Translate a ``thinking`` workflow chunk into its SSE event dict.

    Shared by the chat route and the background run executor so the
    thinking-event shape lives in one place. Sink-agnostic — the caller
    yields it as SSE or pushes it via ``_emit``.
    """
    evt: Dict[str, Any] = {"type": "thinking", "chat_id": chat_id}
    if "delta" in chunk:
        evt["delta"] = chunk.get("delta", "")
    else:
        evt["message"] = chunk.get("message", "正在思考...")
    return evt


def build_tool_call_event(chunk: dict, chat_id: str, tool_calls_log: list) -> Dict[str, Any]:
    """Build the ``tool_call`` SSE event and upsert it into ``tool_calls_log``.

    The log mutation is part of the shared semantics (both call sites upsert
    before emitting). Returns the event dict for the caller to sink.
    """
    tc: Dict[str, Any] = {
        "tool_name": chunk.get("tool_name"),
        "tool_display_name": chunk.get("tool_display_name"),
        "tool_args": chunk.get("tool_args", {}),
        "tool_id": chunk.get("tool_id"),
    }
    if chunk.get("subagent_name"):
        tc["subagent_name"] = chunk["subagent_name"]
    if chunk.get("scope"):
        tc["scope"] = chunk["scope"]
    upsert_tool_call(tool_calls_log, tc)
    return {"type": "tool_call", **tc, "chat_id": chat_id}


def build_tool_result_event(chunk: dict, chat_id: str, tool_calls_log: list) -> Dict[str, Any]:
    """Build the ``tool_result`` SSE event and attach it into ``tool_calls_log``.

    Attach-vs-emit ordering is irrelevant: the log is only read later at
    persist time, and the returned event is fully built before the attach.
    """
    tid = chunk.get("tool_id")
    tn = chunk.get("tool_name")
    res = chunk.get("result", {})
    evt: Dict[str, Any] = {
        "type": "tool_result",
        "tool_name": tn,
        "result": res,
        "tool_id": tid,
        "chat_id": chat_id,
        "citations": chunk.get("citations", []),
    }
    if chunk.get("subagent_name"):
        evt["subagent_name"] = chunk["subagent_name"]
    if chunk.get("scope"):
        evt["scope"] = chunk["scope"]
    attach_tool_result(tool_calls_log, tid, tn, res)
    return evt


def upsert_tool_call(tool_calls_log: list, tc: dict) -> None:
    """Merge a tool_call into the log, updating an existing entry by tool_id."""
    tid = tc.get("tool_id")
    if tid:
        for existing in tool_calls_log:
            if existing.get("tool_id") == tid:
                if tc.get("tool_args"):
                    existing["tool_args"] = tc["tool_args"]
                if tc.get("tool_display_name"):
                    existing["tool_display_name"] = tc["tool_display_name"]
                return
    tool_calls_log.append(tc)


def attach_tool_result(tool_calls_log: list, tid: str, tn: str, res: Any) -> None:
    """Attach a tool_result to the matching tool_call entry in the log."""
    for tc in tool_calls_log:
        if tid and tc.get("tool_id") == tid:
            tc["result"], tc["status"] = res, "success"
            return
        if tn and tc.get("tool_name") == tn and "result" not in tc:
            tc["result"], tc["status"] = res, "success"
            return
    if tid or tn:
        tool_calls_log.append({"tool_name": tn, "tool_id": tid, "result": res, "status": "success"})


# Persistence caps for sub-agent sub-steps (prevent a single call_subagent's sub_steps from growing unbounded and bloating the message row).
_SUBSTEP_OUTPUT_CAP = 16000   # max characters stored per sub-tool result
_SUBSTEP_MAX_STEPS = 200      # max sub-steps stored per call_subagent card


def _upsert_tool_step(steps: list, tid: Any, name: str, patch: dict) -> None:
    """Merge a sub-tool step by toolId: on hit, merge patch (+name); otherwise append (subject to the step-count cap)."""
    for s in steps:
        if s.get("kind") == "tool" and s.get("toolId") == tid:
            s.update(patch)
            if name:
                s["name"] = name
            return
    if len(steps) < _SUBSTEP_MAX_STEPS:
        steps.append({"kind": "tool", "toolId": tid, "name": name or "tool",
                      "status": "running", **patch})


def attach_subagent_step(tool_calls_log: list, parent_tool_id: str, ev: dict) -> None:
    """Accumulate one subagent_event into the ``sub_steps`` of the matching
    call_subagent entry, so the sub-agent's internal process can be replayed
    after a refresh.

    Merge rules mirror the frontend's applySubagentEvent: tool_call is merged
    by tool_id (start has no args → end backfills them), tool_result backfills
    output/status, thinking is merged incrementally. Does **not** persist
    ``content`` (i.e. the sub-agent's answer itself, already in the
    call_subagent tool_result) nor start/end/error control events. Capped to
    prevent bloat.
    """
    if not parent_tool_id:
        return
    entry = None
    for tc in tool_calls_log:
        if tc.get("tool_id") == parent_tool_id:
            entry = tc
            break
    if entry is None:
        return

    if ev.get("agent_name") and not entry.get("subagent_name"):
        entry["subagent_name"] = ev["agent_name"]

    steps = entry.setdefault("sub_steps", [])
    st = ev.get("sub_type")

    if st == "tool_call":
        inp = ev.get("input")
        _upsert_tool_step(steps, ev.get("tool_id"), ev.get("tool_name"),
                          {"input": inp} if inp is not None else {})

    elif st == "tool_result":
        out = ev.get("output")
        if isinstance(out, str) and len(out) > _SUBSTEP_OUTPUT_CAP:
            out = out[:_SUBSTEP_OUTPUT_CAP] + "…（已截断）"
        status = "error" if ev.get("status") == "error" else "success"
        _upsert_tool_step(steps, ev.get("tool_id"), ev.get("tool_name"),
                          {"output": out, "status": status})

    elif st == "thinking":
        delta = ev.get("delta") or ""
        if not delta:
            return
        if steps and steps[-1].get("kind") == "thinking":
            cur = steps[-1].get("text") or ""
            if len(cur) < _SUBSTEP_OUTPUT_CAP:
                steps[-1]["text"] = cur + delta
        elif len(steps) < _SUBSTEP_MAX_STEPS:
            steps.append({"kind": "thinking", "text": delta})
