"""Budget selection works on whole ReAct steps, never on single blocks.

Reasoning is counted on its own but is never shortened or evicted apart from
the step that produced it; a step keeps its tool calls and their results
together; the turn in progress is protected; and pressure is resolved by
pruning old tool outputs before cutting whole steps from the oldest end.
"""

import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from core.llm.chat_models import ReasoningEchoChatFormatter
from core.llm.context_adapter import (
    AgentScopeContextAdapter,
    next_request_sequence,
    render_context_item,
)
from core.llm.context_ir import (
    KIND_THINKING,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_USER_INPUT,
    POLICY_NEVER,
    ContextAssembler,
    ContextItem,
)
from core.llm.message_compat import session_to_msgs
from core.llm.model_steps import record_assistant_step, record_tool_result_step, replay_rows

LONG_THINKING = "深入推理一下这个问题，" * 4_000  # far beyond the old 20k per-block cap


def _request(context, text="现在的问题"):
    seq = next_request_sequence(context)
    return render_context_item(
        ContextItem.create(
            item_id=f"request:{seq}",
            kind=KIND_USER_INPUT,
            origin="user:chat",
            trust="user",
            visibility="model",
            priority=1_000,
            token_budget=100_000,
            truncation_policy=POLICY_NEVER,
            content=text,
            cache_class="dynamic",
            created_seq=seq,
            render_role="user",
            render_name="user",
            message_group=f"request:{seq}",
        )
    )


def _live_turn(thinking="思考", result="结果"):
    """What AgentScope accumulates for one turn: think → call → result → think → answer."""
    return Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking=thinking),
            ToolCallBlock(type="tool_call", id="c1", name="search", input="{}"),
            ToolResultBlock(type="tool_result", id="c1", name="search", output=result),
            ThinkingBlock(type="thinking", thinking="再想"),
            TextBlock(type="text", text="答案"),
        ],
    )


def _assemble(messages, budget):
    adapter = AgentScopeContextAdapter()
    items = adapter.items_from_messages(messages)
    assembly = ContextAssembler(total_budget=budget).assemble(items)
    return adapter, items, assembly


def test_long_thinking_is_never_rewritten_into_text_under_a_tight_budget():
    history = session_to_msgs(
        [
            {"role": "user", "content": "旧问题"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": LONG_THINKING},
                    {"type": "text", "text": "旧答案"},
                ],
            },
        ]
    )
    context = history + [_request(history)]

    adapter, items, assembly = _assemble(context, budget=5_000)

    thinking = next(item for item in items if item.kind == KIND_THINKING)
    assert thinking.truncation_policy == POLICY_NEVER
    assert thinking.token_estimate > 5_000
    rendered = adapter.messages_from_items(assembly.included)
    for message in rendered:
        for block in message.get_content_blocks():
            if block.type == "text":
                assert "thinking" not in block.text and "omitted" not in block.text
    # The step did not fit: it left whole, recorded as a budget cut, not a rewrite.
    actions = {entry["item_id"]: entry for entry in assembly.manifest["excluded"]}
    assert actions[thinking.item_id]["reason"] == "budget_cut"
    assert all(
        entry["action"] != "truncated"
        for entry in assembly.manifest["included"]
        if entry["kind"] == KIND_THINKING
    )


def test_thinking_is_counted_separately_but_kept_with_its_step():
    live = _live_turn()
    context = [_request([])] + [live]

    adapter, items, assembly = _assemble(context, budget=200_000)

    by_kind = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)
    step_units = {item.unit_id for item in by_kind[KIND_TOOL_CALL]}
    first_thinking = by_kind[KIND_THINKING][0]
    assert first_thinking.unit_id in step_units
    assert by_kind[KIND_TOOL_RESULT][0].unit_id == by_kind[KIND_TOOL_CALL][0].unit_id
    # The second thinking opens the next step (after the result), like the formatter's flush.
    assert by_kind[KIND_THINKING][1].unit_id != first_thinking.unit_id
    assert assembly.manifest["included"][0]["kind"] == KIND_USER_INPUT
    assert {entry["kind"] for entry in assembly.manifest["included"]} >= {
        KIND_THINKING,
        KIND_TOOL_CALL,
        KIND_TOOL_RESULT,
    }


def test_keeping_a_tool_call_keeps_the_reasoning_that_issued_it():
    old = session_to_msgs(
        [
            {"role": "user", "content": "旧问题"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "为什么要查" * 50},
                    {"type": "tool_call", "id": "c0", "name": "search", "input": "{}"},
                ],
            },
            {
                "role": "tool",
                "content": [
                    {"type": "tool_result", "id": "c0", "name": "search", "output": "旧结果"}
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "旧答案"}]},
        ]
    )
    context = old + [_request(old)]
    adapter, items, assembly = _assemble(context, budget=200_000)
    call = next(item for item in items if item.kind == KIND_TOOL_CALL)
    thinking = next(item for item in items if item.kind == KIND_THINKING)

    assert thinking.unit_id == call.unit_id
    included = {item.item_id for item in assembly.included}
    assert {thinking.item_id, call.item_id} <= included

    # Under pressure the whole step leaves together; the call never survives alone.
    _, _, squeezed = _assemble(context, budget=60)
    included = {item.item_id for item in squeezed.included}
    assert thinking.item_id not in included and call.item_id not in included
    assert not any(item.kind == KIND_TOOL_RESULT for item in squeezed.included)


def test_the_turn_in_progress_is_protected_and_over_budget_is_explicit():
    old = session_to_msgs(
        [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": [{"type": "text", "text": "旧答案"}]},
        ]
    )
    context = (
        old + [_request(old)] + [_live_turn(thinking="很长的推理" * 400, result="很长的结果" * 400)]
    )

    adapter, items, assembly = _assemble(context, budget=100)

    rendered = adapter.messages_from_items(assembly.included)
    kinds = [item.kind for item in assembly.included]
    assert KIND_USER_INPUT in kinds and KIND_TOOL_CALL in kinds and KIND_TOOL_RESULT in kinds
    assert assembly.over_budget is True
    assert assembly.manifest["protected_units"] == 2
    # Old history was cut; the live turn was neither pruned nor cut.
    assert assembly.cut_units == 2 and assembly.pruned_items == 0
    result = next(b for m in rendered for b in m.get_content_blocks() if b.type == "tool_result")
    assert result.output == "很长的结果" * 400


def test_old_tool_outputs_are_pruned_before_old_steps_are_cut():
    rows = []
    for index in range(3):
        rows += [
            {"role": "user", "content": f"问题{index}"},
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": f"c{index}", "name": "s", "input": "{}"}],
            },
            {
                "role": "tool",
                "content": [
                    {"type": "tool_result", "id": f"c{index}", "name": "s", "output": "R" * 2_000}
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": f"答案{index}"}]},
        ]
    history = session_to_msgs(rows)
    context = history + [_request(history)]
    adapter, items, full = _assemble(context, budget=200_000)
    full_cost = full.used_tokens

    # Room for everything except one raw tool output → the oldest output is pruned, no step is cut.
    _, _, pruned = _assemble(context, budget=full_cost - 400)
    assert pruned.pruned_items == 1 and pruned.cut_units == 0
    pruned_entry = next(e for e in pruned.manifest["included"] if e["action"] == "pruned")
    assert pruned_entry["item_id"].startswith("message:2:")
    rendered = adapter.messages_from_items(pruned.included)
    outputs = [
        b.output for m in rendered for b in m.get_content_blocks() if b.type == "tool_result"
    ]
    assert "pruned" in outputs[0] and outputs[1] == "R" * 2_000

    # Room for only the newest turn → older steps are cut from the oldest end, contiguously.
    _, _, cut = _assemble(context, budget=120)
    assert cut.cut_units > 0
    included_ids = [item.item_id for item in cut.included]
    assert not any(i.startswith("message:0:") for i in included_ids)
    excluded_ids = [e["item_id"] for e in cut.manifest["excluded"]]
    assert all(e["reason"] == "budget_cut" for e in cut.manifest["excluded"])
    assert excluded_ids == sorted(excluded_ids, key=lambda i: int(i.split(":")[1]))


@pytest.mark.asyncio
async def test_in_memory_turn_and_its_replayed_record_produce_the_same_wire_messages():
    """What the agent held live and what the record replays must reach the provider identically."""
    live = _live_turn(thinking="思考A", result="结果A")
    steps = [
        record_assistant_step(
            [
                ThinkingBlock(type="thinking", thinking="思考A"),
                ToolCallBlock(type="tool_call", id="c1", name="search", input="{}"),
            ],
            provider="deepseek",
            model="v4",
            protocol="openai_chat",
        ),
        record_tool_result_step(
            ToolResultBlock(type="tool_result", id="c1", name="search", output="结果A")
        ),
        record_assistant_step(
            [ThinkingBlock(type="thinking", thinking="再想"), TextBlock(type="text", text="答案")],
            provider="deepseek",
            model="v4",
            protocol="openai_chat",
        ),
    ]
    replayed = session_to_msgs(replay_rows(steps))

    formatter = ReasoningEchoChatFormatter()
    assert await formatter.format([live]) == await formatter.format(replayed)


@pytest.mark.asyncio
async def test_switching_providers_never_forwards_the_old_signature():
    steps = [
        record_assistant_step(
            [
                ThinkingBlock(type="thinking", thinking="思考", signature="anthropic-sig"),
                TextBlock(type="text", text="答"),
            ],
            provider="anthropic",
            model="claude",
            protocol="anthropic_messages",
        )
    ]
    rows = await ReasoningEchoChatFormatter().format(session_to_msgs(replay_rows(steps)))

    assert rows[0]["reasoning_content"] == "思考"
    assert "anthropic-sig" not in str(rows)
    assert "signature" not in rows[0]
