"""Message format conversion between dict messages and AgentScope Msg objects."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from core.llm.context_adapter import AgentScopeContextAdapter


def _wrap_content(content: Any) -> list:
    """In 2.0, Msg.content must be a list of blocks (bare str not accepted). Wrap str as [TextBlock]."""
    return AgentScopeContextAdapter.wrap_content(content)


def dict_to_msg(d: Dict[str, Any], *, created_seq: int = 0) -> Any:
    """Convert a dict message (OpenAI format) to an AgentScope Msg.

    ``content`` may be either a ``str`` or a ``list[ContentBlock]`` —
    AgentScope's :class:`Msg` accepts both. The "tool" role (used by the
    structured tool-call replay path to mark a tool_result carrier) maps
    to ``role="user"`` since AgentScope Msg only supports user/assistant/system.
    """
    role = d.get("role", "user")
    content = d.get("content", "")
    name = d.get("name", role)

    # Map roles: "human" -> "user", "ai"/"assistant" -> "assistant".
    # ⚠️ AgentScope 2.0: tool_call / tool_result blocks may **only** be attached
    # to assistant messages (user allows only text/data, system only text). The
    # 1.x practice of putting tool_result on "tool"→"user" is rejected by Msg
    # validation in 2.0, so "tool" now maps to "assistant".
    # (The dict layer still keeps the "tool" marker so compaction can tell a
    # result carrier from a step boundary — see core/llm/model_steps.py.)
    role_map = {"human": "user", "ai": "assistant", "tool": "assistant"}
    role = role_map.get(role, role)

    # Ensure valid role
    if role not in ("user", "assistant", "system"):
        role = "user"

    # AgentScope construction is centralized in AgentScopeContextAdapter so
    # provenance metadata survives history replay and final request assembly.
    prepared = dict(d)
    prepared.update(role=role, name=name, content=content)
    return AgentScopeContextAdapter().message_from_session_dict(
        prepared,
        created_seq=created_seq,
    )


def msg_to_dict(msg: Any) -> Dict[str, Any]:
    """Convert an AgentScope Msg to a dict message (OpenAI format)."""
    return {
        "role": msg.role,
        "content": msg.get_text_content(),
    }


def session_to_msgs(session_messages: List[Dict[str, Any]]) -> List[Any]:
    """Convert dict session messages into list[Msg] for writing into agent.state.context.

    AgentScope 2.0 removed the memory module; callers use
    ``agent.state.context.extend(session_to_msgs(history))`` instead of 1.x's
    ``await load_session_into_memory(history, agent.memory)``.
    """
    return [
        dict_to_msg(message, created_seq=index)
        for index, message in enumerate(session_messages)
        if message.get("content")
    ]


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
            msg.get_content_blocks("tool_call") if msg.has_content_blocks("tool_call") else []
        )
        tool_result_blocks = (
            msg.get_content_blocks("tool_result") if msg.has_content_blocks("tool_result") else []
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
