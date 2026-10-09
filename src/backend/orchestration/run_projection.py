"""Materialized live message owned by the ordered run event log.

This projection never reads the asynchronously persisted chat-history row.
Its offset advances only in the same transaction that appends its event.
"""


def new_projection(run_id):
    return {"run_id": run_id, "message_id": "", "started_at": None,
            "event_offset": 0, "blocks": [], "tools": [], "signals": {},
            "terminal": False, "history": [], "citations": []}


def _text(state, kind, content, **fields):
    if not content:
        return
    blocks = state["blocks"]
    if blocks and blocks[-1]["kind"] == kind and all(blocks[-1].get(k) == v for k, v in fields.items()):
        blocks[-1]["content"] += content
    else:
        blocks.append({"kind": kind, "content": content, **fields})


def _tool(state, event):
    tools = state["tools"]
    identity = str(event.get("tool_id") or "")
    index = next((i for i, t in enumerate(tools) if identity and t.get("id") == identity), -1)
    if index < 0:
        index = len(tools)
        tools.append({"id": identity or None, "name": event.get("tool_name") or "",
                      "displayName": event.get("tool_display_name"), "input": {}, "status": "running",
                      "timestamp": event.get("started_at") or event.get("server_ts")})
        state["blocks"].append({"kind": "tool", "index": index})
    return tools[index]


def _child(tool, event):
    steps = tool.setdefault("subSteps", [])
    kind = event.get("sub_type")
    tid = str(event.get("tool_id") or "")
    if kind in {"content", "thinking"}:
        if steps and steps[-1]["kind"] == kind:
            steps[-1]["text"] += str(event.get("delta") or "")
        else:
            steps.append({"kind": kind, "text": str(event.get("delta") or "")})
    elif kind in {"tool_call", "tool_call_start", "tool_call_delta", "tool_result"}:
        step = next((s for s in steps if s["kind"] == "tool" and tid and s.get("toolId") == tid), None)
        if step is None:
            step = {"kind": "tool", "toolId": tid, "name": event.get("tool_name") or "", "status": "running"}
            steps.append(step)
        if kind == "tool_call":
            step["input"] = event.get("input", event.get("tool_args", {}))
        if kind == "tool_call_delta":
            step["inputText"] = step.get("inputText", "") + str(event.get("arguments_delta") or "")
        if kind == "tool_result":
            step.update(output=event.get("output", event.get("result")), status=event.get("status") or "success")
    elif kind == "end":
        tool["status"] = "interrupted" if event.get("status") == "cancelled" else "success" if event.get("ok") else "error"
        for step in steps:
            if step["kind"] == "tool" and step.get("status") == "running":
                step["status"] = "interrupted"
    if event.get("agent_name"):
        tool["subagentName"] = event["agent_name"]



def _signal(state, event):
    kind = event.get("type", "")
    identity = event.get("request_id") or event.get("confirm_id") or event.get("tool_id") or ""
    scope = event.get("scope") or ""
    if kind == "ontology_activation":
        identity = f"{event.get('pack_id')}:{event.get('workflow_id')}"
    elif kind == "ontology_gate":
        identity = str(event.get("_offset"))
    key = f"{kind}:{scope}:{identity}"
    if kind == "subagent_event":
        identity = f"{event.get('parent_tool_id')}:{event.get('sub_type')}"
        key = f"{kind}:{scope}:{identity}"
    if kind == "user_question_resolved":
        state["signals"].pop(f"user_question::{identity}", None)
        return
    if kind in {"file_confirm", "design_pick"} and event.get("expired"):
        state["signals"].pop(key, None)
        return
    value = {**state["signals"].get(key, {}), **{k: v for k, v in event.items() if not k.startswith("_")}}
    for field in ("delta", "arguments_delta"):
        if field in event and key in state["signals"]:
            value[field] = str(state["signals"][key].get(field) or "") + str(event[field] or "")
    state["signals"][key] = value

def fold_projection(previous, event):
    """Copy-on-write: concurrent appenders never mutate a published base."""
    state = {**previous, "blocks": [dict(b) for b in previous["blocks"]],
             "tools": [dict(t) for t in previous["tools"]],
             "signals": dict(previous["signals"]), "history": list(previous["history"]),
             "citations": list(previous.get("citations", []))}
    kind = event.get("type")
    if kind == "run_started":
        state = new_projection(state["run_id"])
        state.update(message_id=event.get("message_id", ""), started_at=event.get("started_at"))
    elif kind == "steer_applied":
        history = state["history"]
        if event.get("had_assistant_output"):
            previous_message = {k: v for k, v in state.items() if k != "history"}
            previous_message["terminal"] = True
            history.append({"role": "assistant", "state": previous_message})
        if event.get("message"):
            history.append({"role": "user", "message_id": event.get("message_id"),
                            "content": event["message"], "timestamp": event.get("server_ts")})
        state = new_projection(state["run_id"])
        state["history"] = history
        state.update(message_id=event.get("next_assistant_message_id") or "",
                     started_at=event.get("server_ts"))
    elif kind in {"content", "ai_message", "text", "delta"}:
        _text(state, "text", str(event.get("delta") or event.get("content") or event.get("text") or ""))
    elif kind == "thinking":
        if event.get("structured_reasoning") is True and not state.get("structured_reasoning"):
            state["blocks"].append({"kind": "protocol", "structured": True})
            state["structured_reasoning"] = True
        _text(state, "thinking", str(event.get("delta") or event.get("message") or ""),
              structured=event.get("structured_reasoning") is True)
    elif kind == "content_replace":
        state["blocks"].append({"kind": "answer", "content": str(event.get("content") or event.get("text") or "")})
        state["structured_reasoning"] = True
    elif event.get("scope") == "ontology_revision":
        _signal(state, event)
    elif kind in {"tool_call_start", "tool_call_delta", "tool_call", "tool_result"}:
        tool = _tool(state, event)
        if kind == "tool_call":
            tool["input"] = event.get("tool_args", {})
            tool.pop("inputText", None)
        elif kind == "tool_call_delta":
            tool["inputText"] = tool.get("inputText", "") + str(event.get("arguments_delta") or "")
        elif kind == "tool_result":
            tool.update(output=event.get("result"), status=event.get("status") or "success",
                        durationMs=event.get("duration_ms"))
            state["blocks"].append({"kind": "phase"})
            if tool["name"] == "choose_design":
                state["signals"] = {k: v for k, v in state["signals"].items() if v.get("type") != "design_pick"}
    elif kind == "subagent_event" and event.get("sub_type") == "job_progress":
        _signal(state, event)
    elif kind == "subagent_event":
        parent = next((t for t in state["tools"] if t.get("id") == event.get("parent_tool_id")), None)
        if parent is not None:
            parent["subSteps"] = [dict(step) for step in parent.get("subSteps", [])]
            _child(parent, event)
    elif kind == "plan_update":
        state["blocks"].append({"kind": "phase"})
        _signal(state, event)
    elif kind == "__terminal__":
        state["terminal"] = True
    else:
        # Latest run-level state, not an event replay: controls consume it once
        # when a fresh subscription installs the snapshot.
        _signal(state, event)
    for citation in event.get("citations") or []:
        if citation not in state["citations"]:
            state["citations"].append(citation)
    state["last_event_ts"] = event.get("server_ts") or state.get("last_event_ts")
    state["event_offset"] = int(event.get("_offset") or 0)
    return state
