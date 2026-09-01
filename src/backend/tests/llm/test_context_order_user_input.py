"""The live user instruction must stay ahead of the turn it triggered.

``next_request_sequence`` parks the request 64 strides above the context tail so
pre-reply middleware can inject ahead of it. The positional fallback used for
rows without an explicit sequence starts at ``message_index * STRIDE``, so every
tool call and tool result of the current turn used to sort *before* the request.
The model then read its own finished work first and the user's ask last, took
that for a fresh request, and ran the whole turn again.
"""

from agentscope.message import Msg, ToolCallBlock, ToolResultBlock

from core.llm.context_adapter import (
    AgentScopeContextAdapter,
    next_request_sequence,
    render_context_item,
)
from core.llm.context_ir import (
    KIND_USER_INPUT,
    POLICY_NEVER,
    ContextAssembler,
    ContextItem,
)

USER_TEXT = "新建一个 test 文件夹，派 5 个子智能体各生成一份 html 放进去"


def _user_message(context: list) -> Msg:
    seq = next_request_sequence(context)
    return render_context_item(
        ContextItem.create(
            item_id=f"request:user_input:{seq}",
            kind=KIND_USER_INPUT,
            origin="user:chat",
            trust="user",
            visibility="model",
            priority=1_000,
            token_budget=100_000,
            truncation_policy=POLICY_NEVER,
            content=USER_TEXT,
            cache_class="dynamic",
            created_seq=seq,
            render_role="user",
            render_name="user",
            message_group=f"request:user_input:{seq}",
        )
    )


def _react_trace() -> list[Msg]:
    """What AgentScope appends while the turn runs."""
    return [
        Msg(
            name="assistant",
            role="assistant",
            content=[ToolCallBlock(type="tool_call", id="t1", name="bash", input="{}")],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ToolResultBlock(
                    type="tool_result", id="t1", name="bash", output="(empty)"
                )
            ],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ToolCallBlock(
                    type="tool_call", id=f"s{i}", name="call_subagent", input="{}"
                )
                for i in range(5)
            ],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ToolResultBlock(
                    type="tool_result",
                    id=f"s{i}",
                    name="call_subagent",
                    output=f"已完成 report_{i}.html",
                )
                for i in range(5)
            ],
        ),
    ]


def _assembled_roles(context: list[Msg]) -> list[Msg]:
    adapter = AgentScopeContextAdapter()
    assembly = ContextAssembler(total_budget=200_000, budget_details={}).assemble(
        adapter.items_from_messages(context)
    )
    return adapter.messages_from_items(assembly.included)


def test_user_instruction_precedes_the_trace_it_triggered():
    context = [_user_message([])]
    context.extend(_react_trace())

    out = _assembled_roles(context)

    assert out[0].role == "user", "the request must open the turn, not close it"
    assert USER_TEXT in str(out[0].content[0].text)
    assert all(m.role == "assistant" for m in out[1:])


def test_ordering_holds_for_a_second_turn():
    context = [_user_message([])]
    context.extend(_react_trace())
    context.append(_user_message(context))
    context.extend(_react_trace())

    out = _assembled_roles(context)
    user_positions = [i for i, m in enumerate(out) if m.role == "user"]

    assert len(user_positions) == 2
    # Each request opens its own turn: nothing from turn 2 may precede request 2.
    assert user_positions == [0, len(out) // 2]
