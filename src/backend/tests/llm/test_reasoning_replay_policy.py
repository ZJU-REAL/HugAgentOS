import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock
from core.models.providers.registry import get_spec
from core.models.providers.vendor_models import build_native_model


def native(provider, model):
    return build_native_model(
        get_spec(provider),
        model=model,
        temperature=0,
        max_tokens=100,
        base_url="",
        api_key="test",
        context_size=10000,
        stream=False,
    )


@pytest.mark.asyncio
async def test_foreign_reasoning_is_not_an_anthropic_thinking_block():
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(
                thinking="old reasoning", provider="deepseek", model="m", protocol="openai_chat"
            ),
            TextBlock(text="answer"),
        ],
    )
    wire = await native("anthropic", "claude-test").formatter.format([msg])
    assert not any(b.get("type") == "thinking" for row in wire for b in row["content"])
    assert "old reasoning" not in str(wire)
    assert "Historical reasoning" not in str(wire)
    assert msg.content[0].type == "thinking"


@pytest.mark.asyncio
async def test_same_anthropic_model_preserves_signature():
    block = ThinkingBlock(
        thinking="reason",
        signature="original-signature",
        provider="anthropic",
        model="claude-test",
        protocol="anthropic_messages",
    )
    wire = await native("anthropic", "claude-test").formatter.format(
        [Msg(name="assistant", role="assistant", content=[block, TextBlock(text="answer")])]
    )
    assert wire[0]["content"][0] == {
        "type": "thinking",
        "thinking": "reason",
        "signature": "original-signature",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        {},
        {
            "provider": "anthropic",
            "model": "other-model",
            "protocol": "anthropic_messages",
            "signature": "foreign-signature",
        },
    ],
)
async def test_unknown_or_other_model_reasoning_is_excluded(origin):
    block = ThinkingBlock(thinking="reason", **origin)
    wire = await native("anthropic", "claude-test").formatter.format(
        [Msg(name="assistant", role="assistant", content=[block])]
    )
    assert wire == []
    assert "foreign-signature" not in str(wire)


@pytest.mark.asyncio
async def test_same_anthropic_model_missing_signature_fails_explicitly():
    block = ThinkingBlock(
        thinking="reason", provider="anthropic", model="claude-test", protocol="anthropic_messages"
    )
    with pytest.raises(ValueError, match="missing its signature"):
        await native("anthropic", "claude-test").formatter.format(
            [Msg(name="assistant", role="assistant", content=[block])]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, expected_native", [("deepseek", True), ("anthropic", False)])
async def test_echo_formatter_preserves_only_target_model_reasoning(provider, expected_native):
    from core.models.chat_models import ReasoningEchoChatFormatter

    formatter = ReasoningEchoChatFormatter(
        replay_provider="deepseek", replay_model="m", replay_protocol="openai_chat"
    )
    block = ThinkingBlock(
        thinking="reason",
        provider=provider,
        model="m",
        protocol="openai_chat",
        signature="signature",
    )
    wire = await formatter.format(
        [Msg(name="assistant", role="assistant", content=[block, TextBlock(text="answer")])]
    )
    assert (wire[0].get("reasoning_content") == "reason") is expected_native
    assert "signature" not in str(wire)
    assert block.type == "thinking"


def test_new_output_is_stamped_for_live_replay():
    from types import SimpleNamespace

    from core.models.reasoning_replay import stamp_reasoning_origin

    block = ThinkingBlock(thinking="reason")
    stamp_reasoning_origin(
        [block], SimpleNamespace(provider_id="deepseek", model="m", wire_protocol="openai_chat")
    )
    assert (block.provider, block.model, block.protocol) == ("deepseek", "m", "openai_chat")


@pytest.mark.asyncio
async def test_other_protocol_does_not_reuse_native_reasoning():
    from core.models.chat_models import ReasoningEchoChatFormatter

    formatter = ReasoningEchoChatFormatter(
        replay_provider="deepseek", replay_model="m", replay_protocol="openai_chat"
    )
    block = ThinkingBlock(
        thinking="reason",
        provider="deepseek",
        model="m",
        protocol="anthropic_messages",
        signature="foreign",
    )
    wire = await formatter.format(
        [Msg(name="assistant", role="assistant", content=[block, TextBlock(text="answer")])]
    )
    assert "reasoning_content" not in wire[0]
    assert "Historical reasoning" not in str(wire[0]["content"])
    assert "reason" not in str(wire[0]["content"])
