"""工具返回的媒体：原样送达模型，不落盘；送不出时如实回执，不中断这一轮。

送达形式按协议分两种——Responses 折回 ``function_call_output``，Chat 另起一条 user
消息承载（AgentScope 原生做法）。两条线都不得把图换成文件路径或文案。

回归的是这条线上事故：``ReasoningReplayMixin`` 继承 ``FormatterBase`` 后，在各绑定类
的 MRO 里排在厂商 formatter 前面，把 ``input_types`` 盖成基类的 ``["text/plain"]``，
于是 ``read_image`` 取回的图在发出前被换成一行
``<system-reminder>... saved locally at: /tmp/xxx.png</system-reminder>``——像素丢光，
文件还落在 backend 容器（沙箱外，模型根本够不着），且全程无任何日志。
"""

import base64
import io
import json
import logging

import pytest
from agentscope.formatter import (
    DashScopeChatFormatter,
    GeminiChatFormatter,
    OllamaChatFormatter,
    OpenAIChatFormatter,
)
from agentscope.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from core.chat.tool_log import attach_tool_result, upsert_tool_call
from core.llm.chat_models import ReasoningEchoChatFormatter
from core.llm.compacting_agent import msg_to_history_dict
from core.llm.responses_models import ResponsesReplayFormatter
from core.llm.providers import vendor_models as vm
from PIL import Image


def png_bytes(color=(200, 30, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(buffer, format="PNG")
    return buffer.getvalue()


def image_result(call_id, raw, *, media_type="image/png"):
    return Msg(
        name="assistant",
        role="assistant",
        content=[
            ToolCallBlock(
                type="tool_call",
                id=call_id,
                name="read_image",
                input=json.dumps({"file_path": f"/workspace/{call_id}.png"}),
            ),
            ToolResultBlock(
                type="tool_result",
                id=call_id,
                name="read_image",
                output=[
                    TextBlock(type="text", text=json.dumps({"type": "image"})),
                    DataBlock(
                        type="data",
                        source=Base64Source(
                            type="base64",
                            media_type=media_type,
                            data=base64.b64encode(raw).decode("ascii"),
                        ),
                        name=f"{call_id}.png",
                    ),
                ],
            ),
        ],
    )


@pytest.mark.parametrize(
    "ours, upstream",
    [
        (ReasoningEchoChatFormatter, OpenAIChatFormatter),
        (vm.ReplayOpenAIFormatter, OpenAIChatFormatter),
        (vm.ReplayGeminiFormatter, GeminiChatFormatter),
        (vm.ReplayDashScopeFormatter, DashScopeChatFormatter),
        (vm.ReplayOllamaFormatter, OllamaChatFormatter),
    ],
)
def test_replay_mixin_does_not_shadow_vendor_media_capability(ours, upstream):
    """混入 replay mixin 不得改动厂商 formatter 声明的可送媒体类型。"""
    assert ours().input_types == upstream().input_types
    assert "image/*" in ours().supported_input_media_types


@pytest.mark.asyncio
async def test_chat_tool_image_rides_in_a_user_message_after_its_tool_row():
    """Chat 线：图走 AgentScope 的原生做法——紧随 tool 回执的一条 user 消息。"""
    raw = png_bytes()
    convo = [
        Msg(name="user", role="user", content=[TextBlock(type="text", text="看图")]),
        image_result("call-1", raw),
    ]
    before = [msg_to_history_dict(m) for m in convo]
    rows = await ReasoningEchoChatFormatter().format(convo)

    tool_index = next(i for i, r in enumerate(rows) if r.get("role") == "tool")
    assert rows[tool_index]["tool_call_id"] == "call-1"
    carrier = rows[tool_index + 1]
    assert carrier["role"] == "user"
    parts = carrier["content"]
    assert [p["type"] for p in parts] == ["image_url"]
    assert base64.b64decode(parts[0]["image_url"]["url"].split(",", 1)[1]) == raw
    assert "saved locally" not in json.dumps(rows)
    # 载体只活在请求载荷里：对话历史不变，所以界面上看不到这条消息
    assert [msg_to_history_dict(m) for m in convo] == before


@pytest.mark.asyncio
async def test_responses_tool_image_rides_inside_its_own_function_output():
    """Responses 线：图折回 ``function_call_output``，与 call_id 直接绑定。"""
    raw = png_bytes()
    items = await ResponsesReplayFormatter().format(
        [
            Msg(name="user", role="user", content=[TextBlock(type="text", text="看图")]),
            image_result("call-1", raw),
        ]
    )

    outputs = [i for i in items if i.get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "call-1"
    parts = outputs[0]["output"]
    assert [p["type"] for p in parts] == ["input_text", "input_image"]
    assert base64.b64decode(parts[1]["image_url"].split(",", 1)[1]) == raw

    # 没有为了带图而凭空插入的用户回合
    assert not [i for i in items if i.get("role") == "user" and i is not items[0]]
    assert "saved locally" not in json.dumps(items)


@pytest.mark.asyncio
async def test_responses_images_always_carry_detail():
    """``input_image`` 必须带 ``detail``，工具返回的和用户上传的都要。

    回归：AgentScope 只产出 type + image_url，而 OpenAI 的 ResponseInputImageParam
    把 detail 列为必填。官方端点与 DeepSeek 官方都不校验，缺字段在那两家看着正常；
    生产在用的 vLLM 严格校验，整轮请求直接 400（detail Field required）。
    """
    raw = png_bytes()
    upload = Msg(
        name="user",
        role="user",
        content=[
            TextBlock(type="text", text="看这张"),
            DataBlock(
                type="data",
                source=Base64Source(
                    type="base64",
                    media_type="image/png",
                    data=base64.b64encode(raw).decode("ascii"),
                ),
            ),
        ],
    )
    items = await ResponsesReplayFormatter().format([upload, image_result("call-1", raw)])

    images = [
        part
        for item in items
        for part in (item.get("output") if isinstance(item.get("output"), list) else [])
        + (item.get("content") if isinstance(item.get("content"), list) else [])
        if isinstance(part, dict) and part.get("type") == "input_image"
    ]
    assert len(images) == 2, json.dumps(items)[:800]
    assert all(part.get("detail") for part in images), images


@pytest.mark.asyncio
async def test_parallel_tool_images_each_keep_their_own_call_id():
    """并发读图时每张图绑在自己那次调用上，不会串到别的调用。"""
    reds, blues = png_bytes((255, 0, 0)), png_bytes((0, 0, 255))
    items = await ResponsesReplayFormatter().format(
        [image_result("call-a", reds), image_result("call-b", blues)]
    )

    by_call = {
        i["call_id"]: i["output"][1]["image_url"]
        for i in items
        if i.get("type") == "function_call_output"
    }
    assert base64.b64decode(by_call["call-a"].split(",", 1)[1]) == reds
    assert base64.b64decode(by_call["call-b"].split(",", 1)[1]) == blues


@pytest.mark.asyncio
async def test_formatting_never_writes_the_image_to_the_host_filesystem(monkeypatch):
    """格式化过程不得在沙箱外创建任何文件。"""
    import agentscope.formatter._formatter_base as fb

    def forbidden(*args, **kwargs):
        raise AssertionError("tool media must never be written outside the sandbox")

    monkeypatch.setattr(fb.tempfile, "NamedTemporaryFile", forbidden)
    rows = await ReasoningEchoChatFormatter().format([image_result("call-1", png_bytes())])
    assert any(
        part["type"] == "image_url"
        for row in rows
        if isinstance(row.get("content"), list)
        for part in row["content"]
    )


@pytest.mark.asyncio
async def test_media_the_wire_cannot_carry_is_reported_not_silently_dropped(caplog):
    """线路收不下的媒体：如实回执给模型 + 记 error 日志，但不中断这一轮。"""
    with caplog.at_level(logging.ERROR, logger="core.llm.tool_result_media"):
        rows = await ReasoningEchoChatFormatter().format(
            [image_result("call-1", png_bytes(), media_type="video/mp4")]
        )

    tool_row = next(r for r in rows if r.get("role") == "tool")
    # 没有可送的媒体，tool 消息就保持纯文本，不会凭空多出内容块
    assert isinstance(tool_row["content"], str)
    text = tool_row["content"]
    # 模型被明确告知这是失败回执，而不是拿到一条能去读的路径
    assert "media-undeliverable" in text and "video/mp4" in text
    assert "不要据此推断" in text
    assert "saved locally" not in text and "/tmp/" not in text
    # 运维侧看得见
    assert any(r.levelno == logging.ERROR and "video/mp4" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_undeliverable_media_does_not_abort_the_rest_of_the_turn():
    """一份送不出的媒体不影响同一轮里其它调用，也不影响能送的图。"""
    raw = png_bytes()
    rows = await ReasoningEchoChatFormatter().format(
        [
            image_result("call-bad", png_bytes(), media_type="video/mp4"),
            image_result("call-ok", raw),
        ]
    )

    by_call = {r["tool_call_id"]: r["content"] for r in rows if r.get("role") == "tool"}
    assert len(by_call) == 2
    assert "media-undeliverable" in by_call["call-bad"]
    # 能送的那张照常抵达，载在它自己那条 user 消息上
    carried = [
        part
        for row in rows
        if row.get("role") == "user" and isinstance(row.get("content"), list)
        for part in row["content"]
        if part.get("type") == "image_url"
    ]
    assert len(carried) == 1
    assert base64.b64decode(carried[0]["image_url"]["url"].split(",", 1)[1]) == raw


def test_parallel_tool_results_attach_to_their_own_call():
    """8 次并发 read_image：结果按 tool_id 归位，不按名字抢第一条空位。"""
    log = []
    for index in range(8):
        upsert_tool_call(
            log,
            {
                "tool_id": f"call-{index}",
                "tool_name": "read_image",
                "tool_args": {"file_path": f"/workspace/p{index}.png"},
            },
        )
    for index in reversed(range(8)):  # 完成顺序与发起顺序相反
        attach_tool_result(log, f"call-{index}", "read_image", {"source": f"p{index}"})

    assert [(tc["tool_args"]["file_path"], tc["result"]["source"]) for tc in log] == [
        (f"/workspace/p{i}.png", f"p{i}") for i in range(8)
    ]


def test_result_never_steals_an_entry_that_has_a_different_call_id():
    """带 id 的结果找不到同 id 条目时，宁可单独记一条，也不认领别人的调用。"""
    log = []
    upsert_tool_call(log, {"tool_id": "call-a", "tool_name": "read_image", "tool_args": {}})
    attach_tool_result(log, "call-z", "read_image", {"source": "z"})
    assert "result" not in log[0]
    assert log[1]["tool_id"] == "call-z"


def test_same_tool_name_never_closes_an_entry_by_name_alone():
    """同名不是身份。没有 id 可比时就单独记一条，耗时留空，不认领任何在跑的调用。"""
    log = []
    upsert_tool_call(log, {"tool_name": "bash", "tool_args": {"command": "ls"}})
    assert attach_tool_result(log, "", "bash", {"stdout": "ok"}) is None
    assert "result" not in log[0]
    assert log[1]["result"] == {"stdout": "ok"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
