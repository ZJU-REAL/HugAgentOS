"""每一个思考档位在两条线路上都必须能建出模型，并把档位传达到线上。

产品的档位名（fast/turbo/medium/high/max）是界面概念，Responses 的 ``reasoning.effort``
是 API 自己的枚举，两者不是同一套。曾经把档位直接塞进后者：``max`` 不在枚举里，模型在
构造阶段就抛校验错误——用户看到的是「这一档切不了」，而不是某个请求失败。所以这里按档位
逐个构造，把「能不能建出来」钉死。
"""

from __future__ import annotations

from typing import get_args

import pytest

from core.llm.chat_models import build_model_for_mode
from core.llm.responses_models import native_reasoning_effort
from core.services.model_config import ResolvedModelConfig

# build_model_for_mode 认识的全部档位，外加“没选档位”。
MODES = ["fast", "turbo", "medium", "high", "max", None]
PROTOCOLS = ["responses", "chat_completions"]


@pytest.fixture(autouse=True)
def _window(monkeypatch):
    """构造模型要查上下文窗口，这里与档位无关，给个定值即可。"""
    monkeypatch.setattr(
        "core.llm.context_manager.resolve_model_context_window", lambda name: 65536
    )


def _cfg(protocol: str) -> ResolvedModelConfig:
    return ResolvedModelConfig(
        base_url="http://model.test/v1",
        api_key="k",
        model_name="test-model",
        context_length=65536,
        provider="openai_compatible",
        extra={"api_protocol": protocol, "supports_reasoning_effort": True},
    )


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", MODES)
def test_every_thinking_mode_builds_on_both_wires(protocol, mode):
    model = build_model_for_mode(_cfg(protocol), mode=mode, stream=True)

    assert model is not None


@pytest.mark.parametrize("mode", MODES)
def test_the_mode_always_reaches_the_wire_through_chat_template_kwargs(mode):
    """无论档位能否对上 API 枚举，模板参数这一路都必须带着它——两条线路一致。"""
    responses = build_model_for_mode(_cfg("responses"), mode=mode, stream=True)
    chat = build_model_for_mode(_cfg("chat_completions"), mode=mode, stream=True)

    assert (
        responses._extra_body["chat_template_kwargs"]
        == chat._extra_body["chat_template_kwargs"]
    )


def test_thinking_off_is_carried_identically_on_both_wires():
    for mode in ("fast", "turbo"):
        responses = build_model_for_mode(_cfg("responses"), mode=mode, stream=True)
        assert responses._extra_body["chat_template_kwargs"] == {"enable_thinking": False}
        assert responses._reasoning_effort is None


@pytest.mark.parametrize(
    "mode,expected", [("medium", "medium"), ("high", "high"), ("max", "xhigh")]
)
def test_every_thinking_level_reaches_the_wire_as_an_effort(mode, expected):
    """每个开着思考的档位都必须带上 effort。

    这条线上 effort 就是思考开关（实测 DeepSeek 的 Responses 端点只看它，不读
    chat_template_kwargs），少发一次就等于把那一档静默降级成「不思考」。
    """
    model = build_model_for_mode(_cfg("responses"), mode=mode, stream=True)

    assert model._reasoning_effort == expected


def test_a_value_the_upstream_would_reject_is_translated_not_sent():
    """产品叫 max、API 叫 xhigh：翻译它，而不是原样发出去让上游 400。"""
    assert native_reasoning_effort("max") == "xhigh"
    assert native_reasoning_effort("high") == "high"
    assert native_reasoning_effort("definitely-not-a-real-effort") is None
    assert native_reasoning_effort(None) is None


def test_the_vocabulary_is_read_from_the_sdk_declaration():
    from agentscope.model import OpenAIResponseModel

    from core.llm.responses_models import _wire_reasoning_efforts

    declared = get_args(
        OpenAIResponseModel.Parameters.model_fields["reasoning_effort"].annotation
    )
    flattened = {v for arg in declared for v in get_args(arg) if isinstance(v, str)}

    assert _wire_reasoning_efforts() == frozenset(flattened)
