"""Per-model structured_reasoning flag reaches the chat model instance.

Regression pin for "thinking mode high + a turn with no thinking = answer arrives in
one lump". The SSE layer announces the structured-reasoning protocol marker at stream
start only when ``model.structured_reasoning`` is True; without it the frontend, in
thinking mode, buffers content as presumed reasoning until the round-end fallback
marker. Models served behind the generic openai_compatible provider get spec default
False, so admins flag them per model via ``extra_config.structured_reasoning`` —
these tests pin that override path and the DeepSeek preset default.
"""

from core.llm.chat_models import make_chat_model
from core.llm.providers.registry import get_spec


def _mk(**kw):
    return make_chat_model(
        model="test-model",
        temperature=0.6,
        max_tokens=1024,
        timeout=60,
        base_url="http://example.invalid/v1",
        api_key="dummy",
        provider="openai_compatible",
        context_size=4096,
        **kw,
    )


def test_override_true_wins_over_generic_spec_default():
    assert _mk(structured_reasoning=True).structured_reasoning is True


def test_no_override_keeps_generic_spec_default_false():
    assert _mk().structured_reasoning is False


def test_explicit_false_override_stays_false():
    assert _mk(structured_reasoning=False).structured_reasoning is False


def test_deepseek_preset_announces_structured_channel():
    # The official DeepSeek API always delivers reasoning via reasoning_content.
    assert get_spec("deepseek").structured_reasoning is True


def test_openai_preset_still_structured():
    assert get_spec("openai").structured_reasoning is True
