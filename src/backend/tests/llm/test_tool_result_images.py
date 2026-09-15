"""Tool images survive the real ReAct loop and provider message formatting."""

import base64
import io
import json
import random
from dataclasses import replace

import pytest
from PIL import Image
from agentscope.agent import ContextConfig
from agentscope.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    URLSource,
    UserMsg,
)
from agentscope.model import ChatResponse
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk
from core.llm.chat_models import OpenAICompatChatModel, ReasoningEchoChatFormatter
from core.llm.compacting_agent import CompactingAgent
from core.llm.context_ir import IMAGE_TOKEN_RESERVE
from core.llm.offloader import SandboxOffloader
from core.llm.tool_collector import ToolCollector
from core.llm.tools.read_image_tool import register_read_image


class CaptureModel(OpenAICompatChatModel):
    model = "image-regression"
    context_size = 128_000

    def __init__(self, tool_name="read_image", arguments=None):
        self.calls = []
        self.tool_name = tool_name
        self.arguments = (
            arguments if arguments is not None else {"file_path": "/workspace/screenshot.png"}
        )
        # Constructed exactly like production (chat_models.OpenAICompatChatModel):
        # no input_types override. Passing one here is what used to hide the fact
        # that the shipped default had been shadowed down to text-only.
        self.formatter = ReasoningEchoChatFormatter()

    async def __call__(self, messages, tools=None, **kwargs):
        self.calls.append(await self.formatter.format(messages))
        content = (
            [ToolCallBlock(id="image-call", name=self.tool_name, input=json.dumps(self.arguments))]
            if len(self.calls) == 1
            else [TextBlock(text="done")]
        )
        return ChatResponse(content=content, is_last=True)


class MemoryStorage:
    def __init__(self):
        self.files = {}

    async def put_file(self, session, path, data):
        self.files[path] = data


@pytest.fixture
def large_png():
    image = Image.frombytes("RGB", (200, 200), random.Random(0).randbytes(120_000))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def run_tool(toolkit, model, *, streaming=False):
    storage = MemoryStorage()
    agent = CompactingAgent(
        name="reviewer" if streaming else "main",
        system_prompt="Inspect the image.",
        model=model,
        toolkit=toolkit,
        context_config=ContextConfig(tool_result_limit=20_000),
        offloader=SandboxOffloader(storage, "shared-workspace"),
    )
    agent._jx_trigger_ratio = 0.8
    prompt = UserMsg(name="user", content="Inspect the screenshot.")
    if streaming:
        async for _ in agent.reply_stream(prompt):
            pass
    else:
        await agent.reply(prompt)
    assert len(model.calls) == 2
    return model.calls[1], storage.files


def wire_images(messages):
    return [
        block["image_url"]["url"]
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "image_url"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["main-reply", "child-stream"])
async def test_read_image_reaches_model_without_text_offload(monkeypatch, large_png, streaming):
    async def read_bytes(*args, **kwargs):
        return large_png

    monkeypatch.setattr("core.llm.tools.read_image_tool._read_path_bytes", read_bytes)
    collector = ToolCollector()
    register_read_image(collector, chat_id="shared-workspace", user_id="u1", vision_mode="native")
    toolkit = Toolkit(tools=collector.function_tools)
    messages, files = await run_tool(toolkit, CaptureModel(), streaming=streaming)
    images = wire_images(messages)
    assert len(images) == 1, json.dumps(messages)[:2000]
    assert base64.b64decode(images[0].split(",", 1)[1]) == large_png
    assert "<<<TRUNCATED>>>" not in json.dumps(messages)
    assert files == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["", "log entry " * 15_000, '"\n截图' * 30_000],
    ids=["images-only", "long-text", "escaped-unicode"],
)
async def test_mixed_result_preserves_images_and_offloads_only_long_text(large_png, text):
    encoded = base64.b64encode(large_png).decode("ascii")
    image = DataBlock(source=Base64Source(media_type="image/png", data=encoded))
    remote = DataBlock(
        source=URLSource(media_type="image/png", url="https://example.test/shot.png")
    )

    async def screenshots():
        """Read screenshots with optional diagnostic output."""
        return ToolChunk(content=[image, TextBlock(text=text), remote] if text else [image, remote])

    collector = ToolCollector()
    collector.register_tool_function(screenshots)
    messages, files = await run_tool(
        Toolkit(tools=collector.function_tools), CaptureModel("screenshots", {})
    )
    assert wire_images(messages) == [
        "data:image/png;base64," + encoded,
        "https://example.test/shot.png",
    ]
    if text:
        assert len(files) == 1
        saved = next(iter(files.values())).decode("utf-8")
        assert saved and saved in text
        assert encoded not in saved
        assert "https://example.test/shot.png" not in saved
        assert text not in json.dumps(messages, ensure_ascii=False)
    else:
        assert files == {}


@pytest.mark.asyncio
async def test_plain_text_still_offloads_when_over_limit():
    text = "ordinary log line " * 10_000

    async def logs():
        """Return large plain text output."""
        return ToolChunk(content=[TextBlock(text=text)])

    collector = ToolCollector()
    collector.register_tool_function(logs)
    messages, files = await run_tool(
        Toolkit(tools=collector.function_tools), CaptureModel("logs", {})
    )
    assert wire_images(messages) == []
    assert len(files) == 1
    assert next(iter(files.values())).decode("utf-8") in text
    assert "<<<TRUNCATED>>>" in json.dumps(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["main-reply", "child-stream"])
@pytest.mark.parametrize(
    "compaction_enabled", [False, True], ids=["sdk-compaction", "shared-compaction"]
)
async def test_large_image_does_not_trigger_false_context_compaction(
    monkeypatch, streaming, compaction_enabled
):
    import core.config.settings as settings_module
    from core.config.settings import settings
    from core.services import compaction_service

    buffer = io.BytesIO()
    Image.frombytes("RGB", (512, 512), random.Random(1).randbytes(512 * 512 * 3)).save(
        buffer, format="PNG"
    )
    png = buffer.getvalue()
    assert len(base64.b64encode(png)) // 4 > CaptureModel.context_size
    attempts = []

    async def unexpected_summary(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Image bytes must not trigger text compaction")

    async def read_bytes(*args, **kwargs):
        return png

    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings, compaction=replace(settings.compaction, enabled=compaction_enabled)),
    )
    monkeypatch.setattr(compaction_service, "_summarize", unexpected_summary)
    monkeypatch.setattr("core.llm.tools.read_image_tool._read_path_bytes", read_bytes)
    model = CaptureModel()
    monkeypatch.setattr(model, "generate_structured_output", unexpected_summary)
    collector = ToolCollector()
    register_read_image(collector, chat_id="shared-workspace", user_id="u1", vision_mode="native")

    messages, files = await run_tool(
        Toolkit(tools=collector.function_tools), model, streaming=streaming
    )

    assert attempts == []
    images = wire_images(messages)
    assert len(images) == 1
    assert base64.b64decode(images[0].split(",", 1)[1]) == png
    assert files == {}


def test_history_estimate_and_summary_do_not_treat_image_as_text():
    from core.llm import compaction as C
    from core.services.compaction_service import estimate_history_tokens

    encoded = "A" * 800_000
    history = [
        {"role": "user", "content": "Inspect the screenshot"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_call", "id": "shot", "name": "read_image", "input": "{}"},
                {
                    "type": "tool_result",
                    "id": "shot",
                    "name": "read_image",
                    "output": [
                        {"type": "text", "text": "Screenshot metadata"},
                        {
                            "type": "data",
                            "name": "/workspace/shot.png",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": encoded,
                            },
                        },
                    ],
                },
            ],
        },
    ]

    measured = estimate_history_tokens(history)
    assert IMAGE_TOKEN_RESERVE <= measured < IMAGE_TOKEN_RESERVE + 1_000
    older, recent = C.split_history_for_compaction(history, keep_recent_tokens=20_000)
    assert older == []
    assert encoded in json.dumps(recent)
    rendered = C.render_content_for_summary(history[1]["content"])
    assert "/workspace/shot.png" in rendered
    assert "Screenshot metadata" in rendered
    assert encoded not in rendered


def jx_chat_model_classes() -> list[type]:
    """Every chat model this repo defines, discovered rather than listed.

    A hand-maintained tuple is how the gap this test guards got in: the failover
    facade was written without the image reserve and no list mentioned it. Import
    the model modules, then walk the SDK base's subclasses so a new model class
    is covered the day it is written.
    """
    import importlib

    from agentscope.model import ChatModelBase

    for module in (
        "core.llm.chat_models",
        "core.llm.failover",
        "core.llm.providers.vendor_models",
        "core.llm.responses_models",
    ):
        importlib.import_module(module)

    seen: list[type] = []

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            if sub.__module__.startswith("core.llm") and sub not in seen:
                seen.append(sub)
            walk(sub)

    walk(ChatModelBase)
    return seen


@pytest.mark.asyncio
async def test_all_provider_estimators_reserve_images_without_mutating_messages():
    image = DataBlock(source=Base64Source(media_type="image/png", data="A" * 800_000))
    messages = [
        Msg(
            name="user",
            role="user",
            content=[
                TextBlock(text="inspect"),
                image,
            ],
        ),
        Msg(
            name="agent",
            role="assistant",
            content=[
                ToolResultBlock(id="shot", name="read_image", output=[image]),
            ],
        ),
    ]
    before = [message.model_dump() for message in messages]
    model_types = jx_chat_model_classes()
    assert model_types, "no repo model classes discovered"
    for model_type in model_types:
        model = object.__new__(model_type)
        estimate = await model.count_tokens(messages=messages, tools=[])
        floor = 2 * IMAGE_TOKEN_RESERVE
        assert floor <= estimate < floor + 52, model_type.__name__
    assert [message.model_dump() for message in messages] == before
