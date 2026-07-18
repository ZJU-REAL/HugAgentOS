"""Message format conversion between dict messages and AgentScope Msg objects."""

from __future__ import annotations

import json
from typing import Any, Dict, List

# AgentScope 2.0: the agentscope.memory module was removed; context lives in agent.state.context: list[Msg].
from agentscope.message import Msg, TextBlock


def _wrap_content(content: Any) -> list:
    """In 2.0, Msg.content must be a list of blocks (bare str not accepted). Wrap str as [TextBlock]."""
    if isinstance(content, str):
        return [TextBlock(type="text", text=content)] if content else []
    if isinstance(content, list):
        return content
    # Anything else (dict block / None): stay as compatible as possible
    return [content] if content else []


def dict_to_msg(d: Dict[str, Any]) -> Msg:
    """Convert a dict message (OpenAI format) to an AgentScope Msg.

    ``content`` may be either a ``str`` or a ``list[ContentBlock]`` —
    AgentScope's :class:`Msg` accepts both. The "tool" role (used by the
    structured tool-call replay path to mark a tool_result carrier) maps
    to ``role="user"`` since AgentScope Msg only supports user/assistant/system.

    Assistant content with raw ``<think>...</think>`` blocks (saved verbatim by
    the streaming path so the frontend can render thinking display) is stripped
    before feeding into agent memory: past thinking is internal reasoning, not
    something the next-turn agent should see — keeping it would (1) bloat the
    prompt with content that can dwarf the actual answer, (2) confuse the model
    when raw think tags appear in a non-thinking position, and (3) potentially
    echo back into the new response. ``run_chat_workflow`` already strips on
    the non-streaming save path, so this load-time strip aligns both paths.
    """
    role = d.get("role", "user")
    content = d.get("content", "")
    name = d.get("name", role)

    # Map roles: "human" -> "user", "ai"/"assistant" -> "assistant".
    # ⚠️ AgentScope 2.0: tool_call / tool_result blocks may **only** be attached
    # to assistant messages (user allows only text/data, system only text). The
    # 1.x practice of putting tool_result on "tool"→"user" is rejected by Msg
    # validation in 2.0, so "tool" now maps to "assistant".
    # (The dict layer still keeps the "tool" marker; trim_history uses it to
    # skip turn boundaries — see build_replay_dicts.)
    role_map = {"human": "user", "ai": "assistant", "tool": "assistant"}
    role = role_map.get(role, role)

    # Ensure valid role
    if role not in ("user", "assistant", "system"):
        role = "user"

    # Strip leftover thinking blocks from past assistant turns. Only touch
    # string content — multimodal (list[block]) assistant messages don't go
    # through the SSE save path that injects raw <think>.
    if role == "assistant" and isinstance(content, str) and content:
        content = strip_thinking(content)

    # 2.0: content must be a list of blocks.
    return Msg(name=name, content=_wrap_content(content), role=role)


def msg_to_dict(msg: Msg) -> Dict[str, Any]:
    """Convert an AgentScope Msg to a dict message (OpenAI format)."""
    return {
        "role": msg.role,
        "content": msg.get_text_content(),
    }


def session_to_msgs(session_messages: List[Dict[str, Any]]) -> List[Msg]:
    """Convert dict session messages into list[Msg] for writing into agent.state.context.

    AgentScope 2.0 removed the memory module; callers use
    ``agent.state.context.extend(session_to_msgs(history))`` instead of 1.x's
    ``await load_session_into_memory(history, agent.memory)``.
    """
    return [dict_to_msg(m) for m in session_messages if m.get("content")]


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output.

    Some thinking models (e.g. DeepSeek R1) emit reasoning wrapped in
    ``<think>...</think>`` tags.  The opening ``<think>`` may be absent.
    """
    if not text:
        return text
    last_end = text.rfind("</think>")
    if last_end != -1:
        return text[last_end + len("</think>"):].lstrip()
    return text


def flatten_tool_output(output: Any) -> str:
    """Flatten a ToolResultBlock.output into plain text.

    In AgentScope 2.0, ``output`` may be a ``str`` or ``list[TextBlock|dict|str]``
    (TextBlock is a pydantic object; dict is the history-replay form). Extract
    the text uniformly and join. Reused by middlewares / summarization and
    other call sites, so each doesn't hand-roll the same traversal.
    """
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, dict):
                text_val = item.get("text")
                parts.append(str(text_val) if text_val is not None else str(item))
            elif getattr(item, "type", None) == "text":
                parts.append(getattr(item, "text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(output) if output is not None else ""


def _format_tool_output(output: Any) -> str:
    """Format tool output for inclusion in shared context messages."""
    if isinstance(output, str):
        return output[:2000] if len(output) > 2000 else output
    # In 2.0, output is often list[TextBlock|dict] (pydantic blocks); json.dumps
    # would fail and degrade to repr. Extract text via flatten_tool_output
    # first, then truncate.
    if isinstance(output, list):
        text = flatten_tool_output(output)
        return text[:2000] if len(text) > 2000 else text
    try:
        text = json.dumps(output, ensure_ascii=False)
        return text[:2000] if len(text) > 2000 else text
    except (TypeError, ValueError):
        return str(output)[:2000]


def _shrink_value(value: Any, max_text: int, max_list: int = 20) -> Any:
    """Recursively truncate long string fields inside dict/list, preserving structure.

    Keeps short scalar fields (ok/file_id/error/size/...) intact while replacing
    long text payloads (content/diff/output) with a truncated marker. Used to
    pack historical tool_calls back into multi-turn context without ballooning
    the prompt.
    """
    if isinstance(value, str):
        if len(value) <= max_text:
            return value
        return value[:max_text] + f"… <truncated, total {len(value)} chars>"
    if isinstance(value, dict):
        return {k: _shrink_value(v, max_text, max_list) for k, v in value.items()}
    if isinstance(value, list):
        head = [_shrink_value(v, max_text, max_list) for v in value[:max_list]]
        if len(value) > max_list:
            head.append(f"… <{len(value) - max_list} more items>")
        return head
    return value


def replay_tool_calls_as_blocks(
    tool_calls: Any,
    *,
    max_args_chars: int = 500,
    max_result_chars: int = 1000,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Convert a persisted ``tool_calls`` JSONB into AgentScope content blocks.

    Returns ``(tool_use_blocks, tool_result_blocks)``.  ``tool_use_blocks`` go
    on the replayed assistant message; ``tool_result_blocks`` go on a
    follow-up ``role="tool"`` carrier message.  Both are paired by ``id``
    so AgentScope's formatter can stitch them back into provider-specific
    request format.

    Same truncation budget as :func:`serialize_tool_calls_for_history`
    (500 chars args / 1000 chars result by default) — the wire format
    changes but the per-call token cost is essentially identical.

    Falls back to a synthetic ``id`` (``hist_{i+1}``) when the DB row
    lacks ``tool_id``.  Failed calls prepend a ``[status=…]`` marker so
    the model still sees the failure signal.
    """
    if not tool_calls or not isinstance(tool_calls, list):
        return [], []

    use_blocks: list[Dict[str, Any]] = []
    result_blocks: list[Dict[str, Any]] = []

    for i, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        name = call.get("tool_name") or call.get("name") or "unknown_tool"
        tool_id = call.get("tool_id") or call.get("id") or f"hist_{i + 1}"
        status = call.get("status") or "unknown"

        # Args: keep dict structure; shrink long strings inside.
        raw_args = call.get("tool_args") if "tool_args" in call else call.get("input")
        shrunk_args = _shrink_value(raw_args if raw_args is not None else {}, max_args_chars)
        if not isinstance(shrunk_args, dict):
            shrunk_args = {"value": shrunk_args}

        # Result: shrink, then serialize to a single string for ToolResultBlock.output.
        # (AgentScope's ToolResultBlock.output is `str | List[TextBlock|...]`;
        # str is simpler and round-trips through every provider formatter.)
        raw_result = call.get("result") if "result" in call else call.get("output")
        shrunk_result = _shrink_value(raw_result if raw_result is not None else {}, max_result_chars)
        if isinstance(shrunk_result, str):
            output_text = shrunk_result
        else:
            try:
                output_text = json.dumps(shrunk_result, ensure_ascii=False)
            except (TypeError, ValueError):
                output_text = str(shrunk_result)

        # Preserve failure signal so the model knows the prior call errored.
        if status not in ("success", "ok"):
            output_text = f"[status={status}]\n{output_text}"

        # 2.0: tool_use → tool_call; input must be a JSON string (not a dict).
        try:
            input_str = json.dumps(shrunk_args, ensure_ascii=False)
        except (TypeError, ValueError):
            input_str = json.dumps({"value": str(shrunk_args)}, ensure_ascii=False)
        use_blocks.append({
            "type": "tool_call",
            "id": tool_id,
            "name": name,
            "input": input_str,
        })
        result_blocks.append({
            "type": "tool_result",
            "id": tool_id,
            "name": name,
            "output": output_text,
        })

    return use_blocks, result_blocks


def build_replay_dicts(
    role: str,
    text_content: str,
    tool_calls: Any,
    *,
    max_args_chars: int = 500,
    max_result_chars: int = 1000,
) -> list[Dict[str, Any]]:
    """Build the dict-message sequence for one DB assistant row.

    Output shape:

    * No tool_calls → ``[{role: "assistant", content: text_content}]`` (single str msg)
    * With tool_calls → two dicts:

      - ``{role: "assistant", content: [TextBlock?, ToolUseBlock, ToolUseBlock, …]}``
      - ``{role: "tool", content: [ToolResultBlock, ToolResultBlock, …]}``

    The ``"tool"`` role marks the tool_result carrier so
    :func:`core.llm.context_manager.trim_history` won't treat it as a
    turn boundary (cleanup loop already skips non-user roles).
    :func:`dict_to_msg` maps ``"tool"`` to ``role="user"`` for Msg
    construction — AgentScope only supports user/assistant/system.
    """
    if role != "assistant" or not tool_calls:
        return [{"role": role, "content": text_content}]

    use_blocks, result_blocks = replay_tool_calls_as_blocks(
        tool_calls,
        max_args_chars=max_args_chars,
        max_result_chars=max_result_chars,
    )
    if not use_blocks:
        # All entries were malformed → fall back to plain text.
        return [{"role": role, "content": text_content}]

    assistant_content: list[Dict[str, Any]] = []
    # Replay with tool_calls goes through the block-list path, bypassing
    # dict_to_msg's str branch — historical thinking must be stripped here,
    # otherwise the previous turn's reasoning monologue leaks verbatim into
    # the next turn's context.
    text = strip_thinking(text_content or "").strip()
    if text:
        assistant_content.append({"type": "text", "text": text})
    assistant_content.extend(use_blocks)

    return [
        {"role": "assistant", "content": assistant_content},
        {"role": "tool", "content": result_blocks},
    ]


def extract_messages_from_context(context: List[Msg]) -> list[dict]:
    """Extract messages from agent.state.context (list[Msg]) into a list of dicts, preserving tool-call blocks.

    Used in shared-context scenarios: passing the main agent's context to a
    sub-agent. AgentScope 2.0: content blocks are pydantic models (attribute
    access b.name / b.input / b.output); the tool-call block type was renamed
    from 1.x's "tool_use" to "tool_call", and b.input is already a JSON string.
    """
    messages: list[dict] = []
    for msg in context:
        d: dict[str, str] = {"role": msg.role, "content": msg.get_text_content() or ""}

        tool_call_blocks = (
            msg.get_content_blocks("tool_call")
            if msg.has_content_blocks("tool_call")
            else []
        )
        tool_result_blocks = (
            msg.get_content_blocks("tool_result")
            if msg.has_content_blocks("tool_result")
            else []
        )

        if tool_call_blocks:
            tool_desc = "\n".join(
                f"[调用工具 {getattr(b, 'name', '')}] 参数: {getattr(b, 'input', '') or ''}"
                for b in tool_call_blocks
            )
            d["content"] += f"\n\n{tool_desc}"

        if tool_result_blocks:
            result_desc = "\n".join(
                f"[工具结果 {getattr(b, 'name', '')}]\n{_format_tool_output(getattr(b, 'output', ''))}"
                for b in tool_result_blocks
            )
            d["content"] += f"\n\n{result_desc}"

        messages.append(d)
    return messages


def extract_text_from_chat_response(response: Any) -> str:
    """Extract text content from a ChatResponse or Msg object."""
    # ChatResponse
    content = getattr(response, "content", None)
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
            # 2.0: content blocks are pydantic objects (attribute access)
            elif getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))
        return "".join(text_parts)

    return str(content)
