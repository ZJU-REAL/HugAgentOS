"""Reasoning is budgeted and reported as its own category, not folded into messages.

``BREAKDOWN_KEYS`` always had a ``thinking`` bucket and the frontend already renders it,
but nothing ever filled it: thinking blocks inherited the generic assistant kind, so their
cost was real but invisible — the usage panel showed only messages / tools / system while
reasoning quietly rode along under "messages". Now that reasoning is handed back to the
model on every turn, what it costs has to be visible.
"""

from core.llm.context_adapter import AgentScopeContextAdapter
from core.llm.context_ir import KIND_ASSISTANT, KIND_THINKING, KIND_TOOL_CALL
from core.llm.context_usage import BREAKDOWN_KEYS, _manifest_breakdown

_REASONING = "先查论文再对比专利，" * 20


def _items(content):
    return AgentScopeContextAdapter().items_from_session_dict(
        {"role": "assistant", "content": content}, created_seq=0
    )


def test_thinking_blocks_get_their_own_kind():
    items = _items(
        [
            {"type": "thinking", "thinking": _REASONING},
            {"type": "text", "text": "最终答案"},
            {"type": "tool_call", "id": "c1", "name": "paper_search", "input": "{}"},
        ]
    )
    assert [i.kind for i in items] == [KIND_THINKING, KIND_ASSISTANT, KIND_TOOL_CALL]


def test_reasoning_lands_in_the_thinking_bucket():
    items = _items(
        [
            {"type": "thinking", "thinking": _REASONING},
            {"type": "text", "text": "最终答案"},
            {"type": "tool_call", "id": "c1", "name": "paper_search", "input": "{}"},
        ]
    )
    breakdown = _manifest_breakdown({"included": [i.to_manifest() for i in items]})
    assert set(breakdown) == set(BREAKDOWN_KEYS)
    assert breakdown["thinking"] > 0
    assert breakdown["messages"] > 0
    assert breakdown["tools"] > 0
    # The reasoning here dwarfs the answer; if it were still filed as a message the two
    # would be indistinguishable.
    assert breakdown["thinking"] > breakdown["messages"]


def test_reasoning_round_trips_as_a_thinking_block_not_as_body_text():
    # The IR must keep the block payload, like tool_call/tool_result do. Flattening it to
    # a bare string would make the rebuild step turn reasoning into a TextBlock — the
    # monologue would come back as the assistant's answer.
    adapter = AgentScopeContextAdapter()
    items = _items([{"type": "thinking", "thinking": _REASONING}, {"type": "text", "text": "答案"}])
    msg = adapter.messages_from_items(items)[0]

    kinds = [b.type for b in msg.get_content_blocks()]
    assert kinds == ["thinking", "text"]
    assert msg.get_text_content() == "答案"


def test_a_turn_without_reasoning_reports_an_empty_thinking_bucket():
    items = _items([{"type": "text", "text": "最终答案"}])
    breakdown = _manifest_breakdown({"included": [i.to_manifest() for i in items]})
    assert breakdown["thinking"] == 0
    assert breakdown["messages"] > 0
