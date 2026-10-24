# -*- coding: utf-8 -*-
"""Cross-turn integration tests for context compaction (real SQLite).

Covers: cross-turn tool results not truncated, checkpoint persistence and
consumption, UI list filtering, delete cascading.
"""

import json

from core.llm.context import compaction as C
from tests.llm.test_compaction_integration import _endpoint


def test_summarize_context_overflow_self_heal(monkeypatch):
    """Summary input exceeds the window → drop the oldest history and retry until it fits (aligned with Codex's ContextWindowExceeded handling)."""
    import asyncio

    from core.chat.compaction import summary as summary

    monkeypatch.setattr(summary, "_resolve_summarizer_model", lambda: _endpoint())
    monkeypatch.setattr(summary, "_load_base_system_prompt", lambda: "SYS PROMPT")

    calls = []

    class _FakeResp:
        def __init__(self, status, payload=None, text=""):
            self.status_code = status
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            msgs = json["messages"]
            calls.append(list(msgs))
            if len(calls) <= 2:
                return _FakeResp(400, text="This model's maximum context length is 1000 tokens")
            return _FakeResp(200, payload={"choices": [{"message": {"content": "交接摘要"}}]})

    monkeypatch.setattr(summary.httpx, "AsyncClient", _FakeClient)

    history = [{"role": "user", "content": f"历史消息{i}，" * 30} for i in range(30)]
    out = asyncio.run(summary._summarize(history, timeout=5))
    assert out == "交接摘要"
    assert len(calls) == 3, "两次超窗 + 一次成功"
    assert len(calls[2]) < len(calls[0]), "重试时必须丢掉最旧消息"
    assert calls[2][0]["role"] == "system", "system 头永不丢弃"
    assert calls[2][-1]["content"] == C.SUMMARIZATION_PROMPT, "末尾摘要指令永不丢弃"
    # What gets dropped is the oldest history: the first history message should be gone
    assert "历史消息0" not in "".join(m["content"] for m in calls[2])


def test_summarize_non_context_error_no_retry(monkeypatch):
    """Non-context-overflow errors (e.g. auth failure) skip the self-heal loop; return None after one attempt."""
    import asyncio

    from core.chat.compaction import summary as summary

    monkeypatch.setattr(summary, "_resolve_summarizer_model", lambda: _endpoint())
    monkeypatch.setattr(summary, "_load_base_system_prompt", lambda: "")

    calls = []

    class _FakeResp:
        status_code = 400
        text = "Invalid API key provided"

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(1)
            return _FakeResp()

    monkeypatch.setattr(summary.httpx, "AsyncClient", _FakeClient)
    out = asyncio.run(summary._summarize([{"role": "user", "content": "q"}], timeout=5))
    assert out is None
    assert len(calls) == 1, "非超窗错误不应重试"
