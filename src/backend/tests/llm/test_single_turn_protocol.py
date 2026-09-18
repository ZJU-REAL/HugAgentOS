"""辅助模型调用必须走端点真正说的那条协议。

追问、标题、分类、KB 抽取、压缩、批量都曾各自硬拼 ``/chat/completions``。桌面网关按
``extra_config.api_protocol`` 决定往上游的哪个路径转发，两边一旦不一致就整条 404：本机端
每答完一轮先白打一次网关，追问和标题静默失效。这里钉的就是「路径、请求体形状、取文本」
三者都由同一份协议事实决定。
"""

from __future__ import annotations

import pytest

from core.llm.single_turn import Endpoint, build_request, parse_response, stream_delta, turns

RESPONSES = Endpoint(
    base_url="https://gw.example/api/v1/desktop/capability/gateway/models/p1",
    api_key="sk-x",
    model_name="m",
    api_protocol="responses",
)
CHAT = Endpoint(base_url="https://gw.example/v1", api_key="sk-x", model_name="m", api_protocol="chat_completions")


def _body(endpoint, **kw):
    return build_request(endpoint, turns("问题", "你是助手"), temperature=0.5, **kw)


def test_responses_endpoint_is_called_on_the_responses_path():
    url, _, body = _body(RESPONSES, max_tokens=512)

    assert url.endswith("/responses")
    assert body["input"][0]["content"][0]["type"] == "input_text"
    assert body["max_output_tokens"] == 512
    assert "messages" not in body and "max_tokens" not in body


def test_chat_endpoint_is_called_on_the_chat_path():
    url, _, body = _body(CHAT, max_tokens=512)

    assert url.endswith("/chat/completions")
    assert body["messages"][0] == {"role": "system", "content": "你是助手"}
    assert body["max_tokens"] == 512
    assert "input" not in body


def test_an_unprobed_endpoint_follows_the_platform_default():
    """没探测过的端点跟主对话同一个默认，不在这里另立一套猜测。"""
    unprobed = Endpoint(base_url="https://x/v1", api_key="k", model_name="m")
    assert unprobed.speaks_responses is True


def test_assistant_turns_carry_the_output_block_type():
    """压缩要把历史里的助手发言送回去；Responses 线上那是输出块，类型与输入侧不同。"""
    _, _, body = build_request(
        RESPONSES,
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        temperature=0.3,
    )
    assert [item["content"][0]["type"] for item in body["input"]] == ["input_text", "output_text"]


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        (
            RESPONSES,
            {
                "output": [
                    {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "想"}]},
                    {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
                ],
                "usage": {"input_tokens": 8, "output_tokens": 3},
            },
        ),
        (
            CHAT,
            {
                "choices": [{"message": {"content": "答案"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3},
            },
        ),
    ],
)
def test_both_lines_yield_the_same_text_and_usage(endpoint, payload):
    result = parse_response(endpoint, payload)

    assert result.text == "答案"
    assert result.usage["prompt_tokens"] == 8
    assert result.usage["completion_tokens"] == 3


def test_thinking_never_leaks_into_the_answer():
    assert parse_response(CHAT, {"choices": [{"message": {"content": "<think>推</think>答案"}}]}).text == "答案"


def test_stream_deltas_are_read_per_protocol():
    assert stream_delta(RESPONSES, {"type": "response.output_text.delta", "delta": "你"}) == "你"
    assert stream_delta(RESPONSES, {"type": "response.reasoning_summary_text.delta", "delta": "想"}) == ""
    assert stream_delta(CHAT, {"choices": [{"delta": {"content": "你"}}]}) == "你"
