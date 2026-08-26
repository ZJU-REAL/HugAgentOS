"""用量抽取必须容忍各家 usage 载荷的形状差异。

AgentScope 的 `ChatUsage` 继承 `DictMixin`（`__getattr__ = dict.__getitem__`），
字段缺失时抛 `KeyError` 而不是 `AttributeError`——`getattr(x, name, default)`
兜不住。这曾让每一次带 usage 的流式回复在收尾时整条炸掉，前端一直停在
「深度拥抱中…」。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from core.llm.model_usage import _usage_from_response, instrument_model_usage, model_usage_scope


class _DictMixinUsage(dict):
    """复刻 AgentScope `DictMixin` 的取值语义。"""

    __setattr__ = dict.__setitem__
    __getattr__ = dict.__getitem__


def _agentscope_usage(**kwargs) -> _DictMixinUsage:
    payload = {
        "input_tokens": 0,
        "output_tokens": 0,
        "time": 0.0,
        "cache_creation_input_tokens": 0,
        "cache_input_tokens": 0,
        "type": "chat",
        "metadata": None,
    }
    payload.update(kwargs)
    return _DictMixinUsage(payload)


def _agentscope_response(usage) -> _DictMixinUsage:
    return _DictMixinUsage({"content": [], "is_last": True, "usage": usage})


def test_agentscope_usage_without_input_token_details() -> None:
    """`input_tokens_details` 在 AgentScope 侧根本不存在，不能因此抛错。"""
    response = _agentscope_response(
        _agentscope_usage(
            input_tokens=1200,
            output_tokens=340,
            cache_input_tokens=800,
            cache_creation_input_tokens=64,
        )
    )

    usage = _usage_from_response(response)

    assert usage.prompt_tokens == 1200
    assert usage.completion_tokens == 340
    assert usage.cache_read_tokens == 800
    assert usage.cache_write_tokens == 64


def test_openai_shaped_usage_with_input_token_details() -> None:
    """OpenAI 风格的普通对象仍要走 `input_tokens_details.cached_tokens`。"""

    @dataclass
    class _Details:
        cached_tokens: int

    @dataclass
    class _Usage:
        prompt_tokens: int
        completion_tokens: int
        input_tokens_details: _Details

    @dataclass
    class _Response:
        usage: _Usage

    usage = _usage_from_response(
        _Response(_Usage(prompt_tokens=90, completion_tokens=7, input_tokens_details=_Details(12)))
    )

    assert usage.prompt_tokens == 90
    assert usage.completion_tokens == 7
    assert usage.cache_read_tokens == 12
    assert usage.cache_write_tokens == 0


@pytest.mark.parametrize("response", [None, object(), {"usage": None}])
def test_missing_usage_is_not_an_error(response) -> None:
    usage = _usage_from_response(response)

    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0


def test_instrumented_stream_survives_agentscope_usage() -> None:
    """带 usage 的流要跑到底，且用量真的记进账本，不能在 finally 里炸掉整条流。"""

    class _Model:
        model = "probe"

        async def _call_api(self, model_name: str, **_kwargs):
            async def _stream():
                yield _agentscope_response(None)
                yield _agentscope_response(_agentscope_usage(input_tokens=5, output_tokens=6))

            return _stream()

    class _Recorder:
        def __init__(self) -> None:
            self.attempts: list = []

        def record_attempt(self, attempt):
            self.attempts.append(attempt)
            return attempt

    model = _Model()
    instrument_model_usage(model)
    recorder = _Recorder()

    async def _drain() -> int:
        with model_usage_scope("run-1", recorder):
            stream = await model._call_api("probe")
            return len([item async for item in stream])

    assert asyncio.run(_drain()) == 2
    assert [a.status for a in recorder.attempts] == ["success"]
    assert recorder.attempts[0].usage.prompt_tokens == 5
    assert recorder.attempts[0].usage.completion_tokens == 6
