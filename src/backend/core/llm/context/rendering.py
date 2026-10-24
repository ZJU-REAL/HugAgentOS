"""Render context items into AgentScope state and allocate their sequence numbers."""
from __future__ import annotations
from core.llm.context import adapter
from dataclasses import replace
from typing import Any, Mapping, Sequence
from agentscope.message import Msg, TextBlock
from core.llm.context.ir_constants import CONTEXT_SEQUENCE_STRIDE, POLICY_HEAD_TAIL
from core.llm.context.items import ContextItem, make_text_context_item

def append_state_context_item(state: Any, item: ContextItem) -> Msg:
    """Render and append one explicitly-provenanced item to AgentScope state."""
    messages = adapter.AgentScopeContextAdapter().messages_from_items([item])
    if len(messages) != 1:
        raise ValueError("context item did not render to exactly one message")
    message = messages[0]
    state.context.append(message)
    return message


def render_text_block(text: str) -> TextBlock:
    """Construct an AgentScope text block inside the SDK adapter boundary."""
    return TextBlock(type="text", text=str(text or ""))


def next_context_sequence(messages: Sequence[Any]) -> int:
    """Allocate one monotonic sequence across replay and live injections."""
    max_sequence = -CONTEXT_SEQUENCE_STRIDE
    for message_index, message in enumerate(messages):
        entries = adapter.AgentScopeContextAdapter._metadata_entries(message)
        blocks = list(getattr(message, "content", None) or [])
        for block_index, _block in enumerate(blocks or [None]):
            explicit = entries[block_index] if block_index < len(entries) else {}
            candidate = explicit.get("created_seq")
            if candidate is None:
                candidate = message_index * CONTEXT_SEQUENCE_STRIDE + block_index
            try:
                max_sequence = max(max_sequence, int(candidate))
            except (TypeError, ValueError):
                continue
    return ((max_sequence // CONTEXT_SEQUENCE_STRIDE) + 1) * CONTEXT_SEQUENCE_STRIDE


def next_request_sequence(messages: Sequence[Any]) -> int:
    """Reserve a pre-reply lane above the context tail for the user instruction.

    AgentScope runs ``on_reply`` middleware before it appends ``inputs`` to
    state. File/project attachment middleware can therefore add a few explicit
    context items first without colliding with the request's sequence. The
    sequence no longer decides message order — the assembler emits items in
    context order — it only keeps identifiers distinct and marks recency for
    budget eviction.
    """
    pre_reply_slots = 64
    return next_context_sequence(messages) + pre_reply_slots * CONTEXT_SEQUENCE_STRIDE


def render_context_item(item: ContextItem) -> Msg:
    """Render one IR item for ``Agent.reply`` without mutating agent state."""
    messages = adapter.AgentScopeContextAdapter().messages_from_items([item])
    if len(messages) != 1:
        raise ValueError("context item did not render to exactly one message")
    return messages[0]


def render_session_input(message: Mapping[str, Any], *, created_seq: int) -> Msg:
    """Render one pending positional row at an explicit live request sequence."""
    context_adapter = adapter.AgentScopeContextAdapter()
    items = context_adapter.items_from_session_dict(message, created_seq=0)
    positioned = [
        replace(
            item,
            created_seq=created_seq + index,
            message_group=f"request:{created_seq}",
        )
        for index, item in enumerate(items)
    ]
    rendered = context_adapter.messages_from_items(positioned)
    if len(rendered) != 1:
        raise ValueError("session input did not render to exactly one message")
    return rendered[0]


def append_context_item(agent: Any, item: ContextItem) -> Msg:
    """Append one explicitly-provenanced item through the adapter seam."""
    return append_state_context_item(agent.state, item)


def append_context_text(
    agent: Any,
    text: str,
    *,
    kind: str,
    origin: str,
    trust: str,
    priority: int = 700,
    token_budget: int = 4_000,
    truncation_policy: str = POLICY_HEAD_TAIL,
    render_role: str = "user",
) -> Msg:
    """Append provenanced text without creating AgentScope messages elsewhere."""
    created_seq = next_context_sequence(agent.state.context)
    item = make_text_context_item(
        text,
        item_id=f"{origin}:{created_seq}",
        kind=kind,
        origin=origin,
        trust=trust,
        created_seq=created_seq,
        priority=priority,
        token_budget=token_budget,
        truncation_policy=truncation_policy,
        render_role=render_role,
    )
    return append_context_item(agent, item)
