"""Canonical per-step record of a turn, in the order the model produced it.

One assistant message row keeps two projections of the same turn. The display
columns (``content`` / ``thinking`` / ``tool_calls`` / ``metadata.segments``)
serve the chat UI. ``model_steps`` is the record the model's own history is
rebuilt from: one entry per model response with the exact blocks it returned,
and one entry per tool result as it entered the context. Replaying it yields
the same message sequence the agent held in memory, so a later turn — or a
resumed one — sends the model what it actually said, step by step, instead of
a reassembly guessed from three separate columns.

The record is written by the run as steps complete and read by
``compaction_service._normalize_rows``. Nothing else interprets it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

STEP_SCHEMA = "harness.steps.v1"
STEP_ASSISTANT = "assistant"
STEP_TOOL_RESULT = "tool_result"
STATE_INTERRUPTED = "interrupted"

_CANCELLED_TURN_MARKER = "[本轮回答被用户中断]"
_INTERRUPTED_RESULT = "[tool call was interrupted before it returned a result]"
_MISSING_RESULT = (
    "[tool result was not recorded; execution outcome is unknown (possibly interrupted)]"
)
_MEDIA_OMITTED = "[image omitted from replay history: {media_type}]"
_LEGACY_TOOL_DIGEST_HEADER = "[历史工具调用摘要 — 原始步骤顺序未被记录，以下按调用列表汇总]"


def _plain(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain(dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _persistable_block(block: Any) -> Dict[str, Any]:
    """Model output block → JSON record. Transport ids of text/thinking are noise."""
    data = _plain(block)
    if not isinstance(data, dict):
        raise ValueError(f"model output block must be a mapping, got {type(block).__name__}")
    block_type = str(data.get("type") or "")
    if not block_type:
        raise ValueError("model output block lacks a type")
    if block_type in {"text", "thinking"}:
        data.pop("id", None)
    elif block_type == "tool_call":
        # Runtime bookkeeping (permission state, suggested rules) is not part
        # of what the model said.
        data = {key: data.get(key) for key in ("type", "id", "name", "input")}
    return data


def _persistable_output(output: Any) -> Any:
    """Tool output as the model saw it; binary media is replaced by an explicit note."""
    if isinstance(output, str) or output is None:
        return output
    if not isinstance(output, (list, tuple)):
        raise ValueError(
            f"tool result output must be text or a block list, got {type(output).__name__}"
        )
    blocks: List[Dict[str, Any]] = []
    for item in output:
        data = _plain(item)
        if isinstance(data, str):
            blocks.append({"type": "text", "text": data})
            continue
        if not isinstance(data, dict):
            raise ValueError("tool result output block must be a mapping")
        block_type = str(data.get("type") or "")
        if block_type == "text":
            blocks.append({"type": "text", "text": str(data.get("text") or "")})
        elif block_type == "data":
            source = data.get("source") if isinstance(data.get("source"), dict) else {}
            media_type = str(source.get("media_type") or "unknown")
            blocks.append({"type": "text", "text": _MEDIA_OMITTED.format(media_type=media_type)})
        else:
            raise ValueError(f"tool result output block type {block_type!r} is not recordable")
    return blocks


def record_assistant_step(
    blocks: Iterable[Any],
    *,
    provider: str,
    model: str,
    protocol: str,
) -> Dict[str, Any]:
    """One model response, exactly the blocks the provider returned."""
    return {
        "schema": STEP_SCHEMA,
        "kind": STEP_ASSISTANT,
        "provider": str(provider or ""),
        "model": str(model or ""),
        "protocol": str(protocol or ""),
        "blocks": [_persistable_block(block) for block in blocks],
    }


def record_tool_result_step(block: Any) -> Dict[str, Any]:
    """One tool result as it entered the context (after the runtime's own bounding)."""
    data = _plain(block)
    if not isinstance(data, dict) or str(data.get("type") or "") != "tool_result":
        raise ValueError("a tool result step needs a tool_result block")
    return {
        "schema": STEP_SCHEMA,
        "kind": STEP_TOOL_RESULT,
        "blocks": [
            {
                "type": "tool_result",
                "id": str(data.get("id") or ""),
                "name": str(data.get("name") or ""),
                "output": _persistable_output(data.get("output")),
                "state": str(data.get("state") or "success"),
            }
        ],
    }


def _ordered_result_steps(
    steps: Sequence[Mapping[str, Any]], *, state: str, missing_output: str
) -> List[Dict[str, Any]]:
    """Close each tool-call group before another assistant response.

    Older records may omit a result or append it at the end of the turn.
    Prefer any recorded result to an unknown-outcome placeholder. Work on
    copies so replay never changes database history.
    """
    results = {
        str(block.get("id") or ""): dict(block)
        for step in steps
        for block in step.get("blocks") or []
        if isinstance(block, Mapping) and block.get("type") == "tool_result"
    }
    # The old fallback asserted interruption even for successfully executed
    # tools whose result event was filtered out. Preserve uncertainty.
    for result in results.values():
        if result.get("state") == STATE_INTERRUPTED and result.get("output") == _INTERRUPTED_RESULT:
            result["output"] = _MISSING_RESULT
    out: List[Dict[str, Any]] = []
    pending: Dict[str, Dict[str, Any]] = {}
    moved: set[str] = set()

    def flush() -> None:
        for call_id, call in pending.items():
            result = results.get(call_id)
            if result is not None:
                moved.add(call_id)
            else:
                result = {
                    "type": "tool_result",
                    "id": call_id,
                    "name": str(call.get("name") or ""),
                    "output": missing_output,
                    "state": state,
                }
            out.append({"schema": STEP_SCHEMA, "kind": STEP_TOOL_RESULT, "blocks": [dict(result)]})
        pending.clear()

    for original in steps:
        step = dict(original)
        blocks = [dict(b) for b in step.get("blocks") or [] if isinstance(b, Mapping)]
        if step.get("kind") == STEP_ASSISTANT:
            flush()
            pending.update(
                (str(b.get("id") or ""), b) for b in blocks if b.get("type") == "tool_call"
            )
        elif step.get("kind") == STEP_TOOL_RESULT:
            blocks = [
                dict(results.get(str(b.get("id") or ""), b))
                for b in blocks
                if str(b.get("id") or "") not in moved
            ]
            for block in blocks:
                pending.pop(str(block.get("id") or ""), None)
            if not blocks:
                continue
        step["blocks"] = blocks
        out.append(step)
    flush()
    return out


def close_dangling_calls(
    steps: List[Dict[str, Any]],
    *,
    state: str = STATE_INTERRUPTED,
) -> List[Dict[str, Any]]:
    """Close missing results at their call boundary when a run stops.

    Missing records do not prove whether the tool executed. Preserve any
    recorded outcome and explicitly mark unknown outcomes without replaying
    the tool or moving a placeholder past the next assistant response.
    """
    steps[:] = _ordered_result_steps(steps, state=state, missing_output=_MISSING_RESULT)
    return steps


def replace_final_text(steps: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """The visible answer was rewritten after the fact; the record follows it.

    Only the text of the last model response changes. Reasoning and tool calls
    stay as produced, so the step keeps its shape and its protocol pairing.
    """
    for step in reversed(steps):
        if step.get("kind") != STEP_ASSISTANT:
            continue
        kept = [b for b in step.get("blocks") or [] if b.get("type") != "text"]
        insert_at = len(kept)
        for index, block in enumerate(kept):
            if block.get("type") == "tool_call":
                insert_at = index
                break
        if text:
            kept.insert(insert_at, {"type": "text", "text": text})
        step["blocks"] = kept
        return steps
    if text:
        steps.append(
            record_assistant_step(
                [{"type": "text", "text": text}], provider="", model="", protocol=""
            )
        )
    return steps


def _append_cancel_marker(blocks: List[Dict[str, Any]]) -> None:
    """Mark a turn the user stopped so the next turn does not read it as finished."""
    if blocks and blocks[-1].get("type") == "text":
        text = str(blocks[-1].get("text") or "")
        blocks[-1]["text"] = (
            f"{text}\n\n{_CANCELLED_TURN_MARKER}" if text else _CANCELLED_TURN_MARKER
        )
    else:
        blocks.append({"type": "text", "text": _CANCELLED_TURN_MARKER})


def _thinking_with_origin(block: Mapping[str, Any], step: Mapping[str, Any]) -> Dict[str, Any]:
    stamped = dict(block)
    if step.get("provider"):
        stamped["provider"] = str(step["provider"])
    if step.get("model"):
        stamped["model"] = str(step["model"])
    if step.get("protocol"):
        stamped["protocol"] = str(step["protocol"])
    return stamped


def replay_rows(
    steps: Sequence[Mapping[str, Any]],
    *,
    cancelled: bool = False,
) -> List[Dict[str, Any]]:
    """Rebuild the model-facing rows of one assistant message from its record.

    Assistant steps become assistant rows with their blocks verbatim; tool
    results become ``role="tool"`` carrier rows (``dict_to_msg`` maps that to
    the assistant role AgentScope requires). A call that never received a
    result is closed at its call boundary with an explicit unknown-outcome
    result. A missing record does not prove the tool never ran; replay only
    repairs protocol pairing and never executes a tool.
    """
    steps = _ordered_result_steps(
        steps,
        state=STATE_INTERRUPTED,
        missing_output=_MISSING_RESULT,
    )
    rows: List[Dict[str, Any]] = []
    for step in steps:
        kind = str(step.get("kind") or "")
        blocks = [dict(b) for b in (step.get("blocks") or []) if isinstance(b, Mapping)]
        if kind == STEP_ASSISTANT:
            rows.append(
                {
                    "role": "assistant",
                    "content": [
                        _thinking_with_origin(b, step) if b.get("type") == "thinking" else b
                        for b in blocks
                    ],
                }
            )
        elif kind == STEP_TOOL_RESULT:
            if rows and rows[-1]["role"] == "tool":
                rows[-1]["content"].extend(blocks)
            else:
                rows.append({"role": "tool", "content": blocks})
        else:
            raise ValueError(f"unknown model step kind {kind!r}")
    if cancelled:
        if not rows or rows[-1]["role"] != "assistant":
            rows.append({"role": "assistant", "content": []})
        _append_cancel_marker(rows[-1]["content"])
    return rows


# ── Rows without a step record ───────────────────────────────────────────────


def _tool_call_block(entry: Mapping[str, Any], index: int) -> Dict[str, Any]:
    args = entry.get("tool_args") if "tool_args" in entry else entry.get("input")
    if not isinstance(args, (dict, list, str)) or args is None:
        args = {}
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    return {
        "type": "tool_call",
        "id": str(entry.get("tool_id") or entry.get("id") or f"hist_{index + 1}"),
        "name": str(entry.get("tool_name") or entry.get("name") or "unknown_tool"),
        "input": args,
    }


def _tool_result_block(entry: Mapping[str, Any], index: int) -> Dict[str, Any]:
    result = entry.get("result") if "result" in entry else entry.get("output")
    if isinstance(result, str):
        output = result
    elif result is None:
        output = ""
    else:
        output = json.dumps(result, ensure_ascii=False, default=str)
    status = str(entry.get("status") or ("success" if "result" in entry else STATE_INTERRUPTED))
    if status not in {"success", "ok"}:
        output = f"[status={status}]\n{output}"
    return {
        "type": "tool_result",
        "id": str(entry.get("tool_id") or entry.get("id") or f"hist_{index + 1}"),
        "name": str(entry.get("tool_name") or entry.get("name") or "unknown_tool"),
        "output": output,
        "state": "success" if status in {"success", "ok"} else status,
    }


def rows_from_segments(
    *,
    content: str,
    thinking: Optional[Sequence[Mapping[str, Any]]],
    tool_calls: Optional[Sequence[Mapping[str, Any]]],
    segments: Sequence[Mapping[str, Any]],
    cancelled: bool = False,
) -> List[Dict[str, Any]]:
    """Rebuild steps from the display-order record written while streaming.

    ``metadata.segments`` was written the moment each piece appeared, so it is
    a faithful order: a run of tool segments is one step's calls, and the next
    thinking or text segment starts the following step once those calls have
    returned. Everything here comes from that record; nothing is inferred.
    """
    thinking = list(thinking or [])
    tool_calls = list(tool_calls or [])
    rows: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    pending: List[tuple[int, Mapping[str, Any]]] = []

    def close_step() -> None:
        nonlocal current, pending
        if current:
            rows.append({"role": "assistant", "content": current})
        if pending:
            rows.append(
                {
                    "role": "tool",
                    "content": [_tool_result_block(entry, index) for index, entry in pending],
                }
            )
        current = []
        pending = []

    for segment in segments:
        seg_type = str(segment.get("type") or "")
        if seg_type in {"thinking", "text"}:
            if pending:
                close_step()
            if seg_type == "thinking":
                index = int(segment.get("index", -1))
                if 0 <= index < len(thinking) and thinking[index].get("content"):
                    current.append(
                        {"type": "thinking", "thinking": str(thinking[index]["content"])}
                    )
            else:
                text = str(segment.get("text") or "")
                if text:
                    current.append({"type": "text", "text": text})
        elif seg_type == "tool":
            index = int(segment.get("index", -1))
            if 0 <= index < len(tool_calls):
                current.append(_tool_call_block(tool_calls[index], index))
                pending.append((index, tool_calls[index]))
        else:
            raise ValueError(f"unknown segment type {seg_type!r}")
    close_step()
    if cancelled:
        if not rows or rows[-1]["role"] != "assistant":
            rows.append({"role": "assistant", "content": []})
        _append_cancel_marker(rows[-1]["content"])
    return rows


def legacy_digest_rows(
    *,
    content: str,
    thinking: Optional[Sequence[Mapping[str, Any]]],
    tool_calls: Optional[Sequence[Mapping[str, Any]]],
    cancelled: bool = False,
) -> List[Dict[str, Any]]:
    """A row written before any order was recorded: replay it as a summary.

    Its columns hold what was said and which tools ran, but not how they were
    interleaved. The turn is therefore replayed as one plain assistant message
    — reasoning, answer, and a labelled digest of the tool calls — rather than
    as native tool_call/tool_result pairs whose order would be a guess. The
    digest keeps every result complete; the context budget decides what fits.
    """
    blocks: List[Dict[str, Any]] = []
    for entry in thinking or []:
        if entry.get("content"):
            blocks.append({"type": "thinking", "thinking": str(entry["content"])})
    body = (content or "").strip()
    if body:
        blocks.append({"type": "text", "text": body})
    calls = [entry for entry in (tool_calls or []) if isinstance(entry, Mapping)]
    if calls:
        lines = [_LEGACY_TOOL_DIGEST_HEADER]
        for index, entry in enumerate(calls):
            call = _tool_call_block(entry, index)
            result = _tool_result_block(entry, index)
            lines.append(f"- {call['name']}({call['input']}) → {result['output']}")
        blocks.append({"type": "text", "text": "\n".join(lines)})
    if cancelled:
        _append_cancel_marker(blocks)
    return [{"role": "assistant", "content": blocks}]


def assistant_row_replay(
    *,
    content: str,
    model_steps: Any,
    thinking: Any,
    tool_calls: Any,
    segments: Any,
    cancelled: bool = False,
) -> List[Dict[str, Any]]:
    """Choose the most faithful available record for one persisted assistant row."""
    if isinstance(model_steps, list) and model_steps:
        return replay_rows(model_steps, cancelled=cancelled)
    if isinstance(segments, list) and segments:
        return rows_from_segments(
            content=content or "",
            thinking=thinking if isinstance(thinking, list) else None,
            tool_calls=tool_calls if isinstance(tool_calls, list) else None,
            segments=segments,
            cancelled=cancelled,
        )
    return legacy_digest_rows(
        content=content or "",
        thinking=thinking if isinstance(thinking, list) else None,
        tool_calls=tool_calls if isinstance(tool_calls, list) else None,
        cancelled=cancelled,
    )


# ── Step structure of in-memory history ──────────────────────────────────────


def explode_history_rows(history: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Split accumulated assistant rows into one row per step.

    AgentScope extends one assistant message in place for a whole ReAct loop,
    so a live-context dump can hold think → call → result → think → answer in
    a single row. Compaction and replay reason about steps, so each such row
    is split on the same boundary the formatter flushes on: a step's own
    blocks form an assistant row, the results that answer it a tool row.
    """
    out: List[Dict[str, Any]] = []
    for row in history:
        role = str(row.get("role") or "user")
        content = row.get("content")
        if role != "assistant" or not isinstance(content, list):
            out.append(dict(row))
            continue
        blocks = [dict(b) for b in content if isinstance(b, Mapping)]
        if not any(b.get("type") == "tool_result" for b in blocks):
            out.append(dict(row))
            continue
        current: List[Dict[str, Any]] = []
        results: List[Dict[str, Any]] = []
        for block in blocks:
            if block.get("type") == "tool_result":
                results.append(block)
                continue
            if results:
                out.append({"role": "assistant", "content": current})
                out.append({"role": "tool", "content": results})
                current, results = [], []
            current.append(block)
        if current:
            out.append({"role": "assistant", "content": current})
        if results:
            out.append({"role": "tool", "content": results})
    return out


__all__ = [
    "STATE_INTERRUPTED",
    "STEP_ASSISTANT",
    "STEP_SCHEMA",
    "STEP_TOOL_RESULT",
    "assistant_row_replay",
    "close_dangling_calls",
    "explode_history_rows",
    "legacy_digest_rows",
    "record_assistant_step",
    "record_tool_result_step",
    "replace_final_text",
    "replay_rows",
    "rows_from_segments",
]
