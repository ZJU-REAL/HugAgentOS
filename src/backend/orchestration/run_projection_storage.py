"""Checkpoint fields: changing one child card must not rewrite every card."""
import json


def encode_checkpoint(state, dirty_tools, *, history_dirty):
    metadata = {k: v for k, v in state.items() if k not in {"tools", "history"}}
    metadata["tool_count"] = len(state["tools"])
    fields = {"state": json.dumps(metadata, ensure_ascii=False)}
    for index in dirty_tools:
        fields[f"tool:{index}"] = json.dumps(state["tools"][index], ensure_ascii=False)
    if history_dirty:
        fields["history"] = json.dumps(state["history"], ensure_ascii=False)
    return fields


def decode_checkpoint(fields):
    if not fields or "state" not in fields:
        return None
    state = json.loads(fields["state"])
    count = state.pop("tool_count")
    state["tools"] = [json.loads(fields[f"tool:{index}"]) for index in range(count)]
    state["history"] = json.loads(fields.get("history") or "[]")
    return state


def affected_tools(state, event):
    kind = event.get("type")
    if kind in {"run_started", "steer_applied"}:
        return set(range(len(state["tools"])))
    identity = event.get("parent_tool_id") if kind == "subagent_event" else event.get("tool_id")
    return {index for index, tool in enumerate(state["tools"])
            if identity and tool.get("id") == str(identity)}
