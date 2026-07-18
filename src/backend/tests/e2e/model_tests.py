#!/usr/bin/env python3
"""
Model chat test — uses the config in .env to verify make_chat_model can start a conversation normally.

Usage:
    PYTHONPATH=src/backend python src/backend/tests/model_tests.py
"""

import asyncio
import os
import sys

# ── Paths & environment variables ───────────────────────────────────────────
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from dotenv import load_dotenv
    _env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))
    load_dotenv(_env_path)
except ImportError:
    _env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))
    if os.path.exists(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _v = _v.strip().strip('"').strip("'")
                    os.environ.setdefault(_k.strip(), _v)

from core.llm.chat_models import make_chat_model
from core.llm.message_compat import extract_text_from_chat_response


# ── Output helpers ───────────────────────────────────────────────────────────

def banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    sys.exit(1)


# ── Test cases ───────────────────────────────────────────────────────────────

def test_construct_client() -> None:
    """[1] Construct the OpenAIChatModel client (no request sent)"""
    model_name = os.getenv("BASE_MODEL_NAME", "glm-5")
    base_url   = os.getenv("MODEL_URL", "")
    api_key    = os.getenv("API_KEY", "")

    client = make_chat_model(
        model=model_name,
        temperature=0.7,
        max_tokens=512,
        timeout=30,
        base_url=base_url,
        api_key=api_key,
    )
    assert client is not None, "客户端构造结果不应为 None"
    ok(f"客户端构造成功 — model={model_name!r}, base_url={base_url!r}")


def test_simple_chat() -> str:
    """[2] Send a single message and get the reply"""
    model_name = os.getenv("BASE_MODEL_NAME", "glm-5")
    base_url   = os.getenv("MODEL_URL", "")
    api_key    = os.getenv("API_KEY", "")

    client = make_chat_model(
        model=model_name,
        temperature=0.7,
        max_tokens=256,
        timeout=60,
        base_url=base_url,
        api_key=api_key,
    )

    messages = [{"role": "user", "content": "你好，请用一句话介绍你自己。"}]
    response = asyncio.run(client(messages=messages))
    content = extract_text_from_chat_response(response)

    assert content, "回复内容不应为空"
    ok(f"单轮对话成功\n     回复: {content[:120]}")
    return content


def test_multi_turn_chat() -> None:
    """[3] Multi-turn conversation (system + two human/ai turns)"""
    model_name = os.getenv("BASE_MODEL_NAME", "glm-5")
    base_url   = os.getenv("MODEL_URL", "")
    api_key    = os.getenv("API_KEY", "")

    client = make_chat_model(
        model=model_name,
        temperature=0.5,
        max_tokens=256,
        timeout=60,
        base_url=base_url,
        api_key=api_key,
    )

    # First turn
    messages = [
        {"role": "system", "content": "你是一个简洁的助手，每次回答不超过 30 个字。"},
        {"role": "user", "content": "1 + 1 等于几？"},
    ]
    r1 = asyncio.run(client(messages=messages))
    ans1 = extract_text_from_chat_response(r1)
    assert ans1, "第一轮回复不应为空"
    ok(f"第一轮: {ans1.strip()}")

    # Second turn (carrying the context)
    messages.append({"role": "assistant", "content": ans1})
    messages.append({"role": "user", "content": "那 2 + 2 呢？"})

    r2 = asyncio.run(client(messages=messages))
    ans2 = extract_text_from_chat_response(r2)
    assert ans2, "第二轮回复不应为空"
    ok(f"第二轮: {ans2.strip()}")


def test_disable_thinking_mode() -> None:
    """[4] Construct and call with disable_thinking=True (fast mode)"""
    model_name = os.getenv("BASE_MODEL_NAME", "glm-5")
    base_url   = os.getenv("MODEL_URL", "")
    api_key    = os.getenv("API_KEY", "")

    client = make_chat_model(
        model=model_name,
        max_tokens=8192,
        temperature=0.7,
        timeout=60,
        base_url=base_url,
        api_key=api_key,
        disable_thinking=True,
    )

    messages = [{"role": "user", "content": "你的模型名称是什么？"}]
    response = asyncio.run(client(messages=messages))
    content = extract_text_from_chat_response(response)
    assert content, "快速模式回复不应为空"
    ok(f"快速模式对话成功\n     回复: {content}")


# ── Main entry ───────────────────────────────────────────────────────────────

def main() -> int:
    banner("模型对话测试 — make_chat_model (AgentScope)")

    tests = [
        ("[4] 快速模式（disable_thinking）", test_disable_thinking_mode),
    ]

    for label, fn in tests:
        print(f"\n{label} ...")
        try:
            fn()
        except SystemExit:
            raise
        except Exception as exc:
            fail(f"{label} 失败: {exc}")

    banner("全部测试通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
