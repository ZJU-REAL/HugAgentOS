"""Hook-injected context is part of the model's own history.

Attachment previews, the historical-file listing and reasoning reminders enter
the live context through hooks. The next turn rebuilds history from the stored
rows; if those blocks are missing there, the request stops matching the one the
provider cached at the first such block and everything after it is billed again.
These tests pin the live request and the replayed one to the same bytes.
"""

import json
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock

from core.chat.compaction.history import normalize_rows
from core.llm.context.ir_constants import KIND_USER_INPUT, POLICY_NEVER
from core.llm.context.items import ContextItem
from core.llm.context.rendering import next_request_sequence, render_context_item
from core.llm.runtime.context_hook_adapter import append_hook_context, record_hook_context
from core.llm.runtime.message_compat import session_to_msgs
from core.llm.runtime.model_steps import record_assistant_step, record_tool_result_step
from core.models.chat_models import ReasoningEchoChatFormatter

ATTACHMENT = "[file name]: - sales.csv  (file_id: ua_1)\n[file content begin]\n| 区域 | 销售额 |\n[file content end]"


@pytest.fixture
def media_db(tmp_path, monkeypatch):
    from core.db.models import ToolMediaBlob
    from core.llm.runtime import tool_media_store
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/media.db")
    ToolMediaBlob.__table__.create(engine, checkfirst=True)
    monkeypatch.setattr(tool_media_store, "SessionLocal", sessionmaker(bind=engine))
    tool_media_store.reset_cache()
    yield engine
    tool_media_store.reset_cache()


def _agent(context=()):
    return SimpleNamespace(state=SimpleNamespace(context=list(context), pending_model_steps=[]))


def _item(content, kind="attachment", origin="user:uploaded_files", trust="user"):
    return dict(content=content, kind=kind, origin=origin, trust=trust,
                priority=650, token_budget=8000)


async def _inject(agent, *items, before_input):
    appended = append_hook_context(agent, SimpleNamespace(data={"context_items": list(items)}))
    await record_hook_context(agent, appended, before_input=before_input)


def _request(context, text):
    seq = next_request_sequence(context)
    return render_context_item(ContextItem.create(
        item_id=f"request:{seq}", kind=KIND_USER_INPUT, origin="user:chat", trust="user",
        visibility="model", priority=1_000, token_budget=100_000,
        truncation_policy=POLICY_NEVER, content=text, cache_class="dynamic",
        created_seq=seq, render_role="user", render_name="user",
        message_group=f"request:{seq}",
    ))


def _assistant(*blocks):
    return record_assistant_step(blocks, provider="deepseek", model="v4", protocol="openai_chat")


def _row(role, content, model_steps=None, **extra):
    return SimpleNamespace(role=role, content=content, extra_data=extra, model_steps=model_steps)


async def _wire(messages):
    return await ReasoningEchoChatFormatter().format(messages)


def _drain(agent):
    steps, agent.state.pending_model_steps = agent.state.pending_model_steps, []
    return steps


@pytest.mark.asyncio
async def test_attachment_turn_replays_to_the_request_the_model_was_sent():
    history = [Msg(name="user", role="user", content=[TextBlock(type="text", text="上一问")]),
               Msg(name="assistant", role="assistant", content=[TextBlock(type="text", text="上一答")])]
    agent = _agent(history)
    # on_reply: the attachment preview is appended before AgentScope adds the user's message.
    await _inject(agent, _item(ATTACHMENT), before_input=True)
    agent.state.context.append(_request(agent.state.context, "按区域汇总"))
    answer = TextBlock(type="text", text="汇总完成")
    agent.state.pending_model_steps.append(_assistant(answer))
    agent.state.context.append(Msg(name="assistant", role="assistant", content=[answer]))

    replayed = normalize_rows([
        _row("user", "上一问"),
        _row("assistant", "上一答"),
        _row("user", "按区域汇总"),
        _row("assistant", "汇总完成", model_steps=_drain(agent)),
    ])

    assert await _wire(session_to_msgs(replayed)) == await _wire(agent.state.context)


@pytest.mark.asyncio
async def test_reasoning_reminder_replays_between_the_steps_it_was_injected_into():
    agent = _agent()
    agent.state.context.append(_request(agent.state.context, "查一下"))
    call = ToolCallBlock(type="tool_call", id="c1", name="search", input="{}")
    result = ToolResultBlock(type="tool_result", id="c1", name="search", output="结果")
    agent.state.context.append(Msg(name="assistant", role="assistant",
                                   content=[ThinkingBlock(type="thinking", thinking="先查"), call, result]))
    steps = [_assistant(ThinkingBlock(type="thinking", thinking="先查"), call),
             record_tool_result_step(result)]
    # on_reasoning: a reminder lands after the tool result, before the next model call.
    await _inject(agent, _item("<system-reminder>\n剩 1 轮\n</system-reminder>", kind="reminder",
                               origin="harness:iteration_budget", trust="system"),
                  before_input=False)
    answer = TextBlock(type="text", text="答案")
    agent.state.pending_model_steps.append(_assistant(answer))
    agent.state.context.append(Msg(name="assistant", role="assistant", content=[answer]))

    replayed = normalize_rows([_row("user", "查一下"),
                               _row("assistant", "答案", model_steps=[*steps, *_drain(agent)])])

    assert await _wire(session_to_msgs(replayed)) == await _wire(agent.state.context)


@pytest.mark.asyncio
async def test_steering_is_not_recorded_twice():
    """A steer already has its own user row; recording the injection would duplicate it."""
    agent = _agent()
    await _inject(agent, _item("[用户在当前执行中追加的新指令]\n改成表格", kind="steer",
                               origin="user:steer"), before_input=False)

    assert agent.state.pending_model_steps == []
    assert len(agent.state.context) == 1


@pytest.mark.asyncio
async def test_native_image_attachment_is_kept_by_reference_and_restored(media_db):
    from core.llm.runtime.tool_media_store import hydrate_rows

    image = [{"type": "text", "text": "[用户上传了 1 张图片：a.png]"},
             {"type": "data", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "iVBORw0KGgo="}}]
    agent = _agent()
    await _inject(agent, _item(image, origin="user:uploaded_images"), before_input=True)
    agent.state.pending_model_steps.append(_assistant(TextBlock(type="text", text="看到了")))
    steps = _drain(agent)

    assert "iVBORw0KGgo=" not in json.dumps(steps)
    replayed = normalize_rows([_row("user", "这是什么"),
                               _row("assistant", "看到了", model_steps=steps)])
    assert "iVBORw0KGgo=" not in json.dumps(replayed)
    media = hydrate_rows(replayed)[0]["content"][1]
    assert media["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}
