"""识图状态事件：模型开口前的那几秒要说清楚在干什么。

视觉桥读图是一段纯网络等待（几秒到十几秒）。折在 agent 轮次内部时，前端只能显示笼统的
「深度拥抱中」并干走秒；把它提到模型调用之前做，就能在首尾各发一个 ``vision_progress``，
让轮级状态显示「图像理解中」。

这里锁三件事：什么时候发、什么时候**不发**、以及预转写的结果确实被中间件直接用上
（不能重复打一次网络）。
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

EVIDENCE_JSON = json.dumps(
    {
        "summary": "一张图。",
        "ocr": {"full_text": "MARKER-8801", "lines": []},
        "layout": {"regions": []},
        "semantics": {"scene": "测试", "entities": [], "relations": []},
        "visual": {"dominant_colors": [], "notes": []},
        "uncertainty": [],
    },
    ensure_ascii=False,
)

IMAGE_FILES = [{"file_id": "art-1", "name": "shot.png", "mime_type": "image/png"}]


class _FakeCfg:
    base_url = "http://fake/v1"
    api_key = "k"
    model_name = "fake-vl"
    temperature = 0.6
    max_tokens = 4096
    context_length = 0
    timeout = 30
    provider = "openai_compatible"
    provider_extra: dict = {}
    extra: dict = {}


def _state(**kw):
    base = dict(
        uploaded_files=list(IMAGE_FILES),
        historical_files=[],
        user_id="u1",
        model_provider_id="",
        context=[],
        vision_evidence_text="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def working_bridge(monkeypatch):
    from core.vision import service as svc
    from core.vision.provider import VisionCallResult

    class _FakeProvider:
        def __init__(self, cfg):
            pass

        async def describe(self, *a, **kw):
            return VisionCallResult(text=EVIDENCE_JSON, mode="json_object")

    monkeypatch.setattr(svc, "VisionProvider", _FakeProvider)
    monkeypatch.setattr(svc, "resolve_vision_config", lambda: _FakeCfg())
    monkeypatch.setattr(svc.VisionBridge, "_cache_get", staticmethod(lambda k: _none()))
    monkeypatch.setattr(svc.VisionBridge, "_cache_set", staticmethod(lambda k, r: _none()))
    svc.VisionBridge._instance = None

    from core.llm import hooks

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: PNG_1X1)
    yield
    svc.VisionBridge._instance = None


async def _none():
    return None


def _collect(state):
    """跑一遍 StreamingAgent 的识图前置，收集它 yield 出的事件。"""
    from orchestration.streaming import StreamingAgent

    agent = SimpleNamespace(state=state)
    streaming = StreamingAgent.__new__(StreamingAgent)
    streaming.agent = agent

    async def run():
        return [e async for e in streaming._prepare_vision_evidence(state)]

    return asyncio.run(run())


# ── 发事件的情形 ────────────────────────────────────────────────────────────


def test_emits_running_then_done_around_the_wait(working_bridge, monkeypatch):
    from core.llm import middlewares

    monkeypatch.setattr(middlewares, "_effective_model_supports_vision", lambda st: False)
    state = _state()
    events = _collect(state)

    assert [e[0] for e in events] == ["vision_progress", "vision_progress"]
    start, done = events[0][1], events[1][1]
    assert start["status"] == "running" and start["count"] == 1
    assert start["names"] == ["shot.png"]
    assert done["status"] == "done" and done["ok"] is True
    # 转写结果留在 state 上，供中间件直接注入
    assert "MARKER-8801" in state.vision_evidence_text


def test_done_reports_failure_without_pretending_success(monkeypatch):
    """桥配了但读失败时，done 必须 ok=False —— 前端据此决定要不要提示。"""
    from core.llm import hooks, middlewares
    from core.vision import service as svc
    from core.vision.provider import VisionCallResult

    class _DeadProvider:
        def __init__(self, cfg):
            pass

        async def describe(self, *a, **kw):
            return VisionCallResult(error="boom")

    monkeypatch.setattr(svc, "VisionProvider", _DeadProvider)
    monkeypatch.setattr(svc, "resolve_vision_config", lambda: _FakeCfg())
    monkeypatch.setattr(svc.VisionBridge, "_cache_get", staticmethod(lambda k: _none()))
    monkeypatch.setattr(svc.VisionBridge, "_cache_set", staticmethod(lambda k, r: _none()))
    svc.VisionBridge._instance = None
    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: PNG_1X1)
    monkeypatch.setattr(middlewares, "_effective_model_supports_vision", lambda st: False)

    events = _collect(_state())
    svc.VisionBridge._instance = None
    assert events[-1][1]["ok"] is False


# ── 不该发事件的情形 ────────────────────────────────────────────────────────


def test_silent_when_turn_has_no_images(working_bridge, monkeypatch):
    from core.llm import middlewares

    monkeypatch.setattr(middlewares, "_effective_model_supports_vision", lambda st: False)
    assert _collect(_state(uploaded_files=[{"name": "a.txt", "mime_type": "text/plain"}])) == []


def test_silent_for_natively_multimodal_model(working_bridge, monkeypatch):
    """主模型自己能看图时压根不转写，也就没有「图像理解中」这回事。"""
    from core.llm import middlewares

    monkeypatch.setattr(middlewares, "_effective_model_supports_vision", lambda st: True)
    state = _state()
    assert _collect(state) == []
    assert state.vision_evidence_text == ""


def test_failure_in_pre_pass_still_closes_the_status(monkeypatch):
    """前置整体抛错也必须补一个 done，否则前端会永远停在「图像理解中」。"""
    from core.llm import middlewares

    def _boom(_st):
        raise RuntimeError("db down")

    monkeypatch.setattr(middlewares, "_effective_model_supports_vision", _boom)
    events = _collect(_state())
    assert len(events) == 1
    assert events[0][1]["status"] == "done" and events[0][1]["ok"] is False


# ── 中间件用预转写结果，不重复打网络 ────────────────────────────────────────


def test_middleware_uses_precomputed_text_without_calling_the_model(monkeypatch):
    from core.llm.middlewares import FileContextMiddleware
    from core.vision import service as svc

    calls = {"n": 0}

    class _CountingProvider:
        def __init__(self, cfg):
            calls["n"] += 1

        async def describe(self, *a, **kw):
            from core.vision.provider import VisionCallResult

            return VisionCallResult(text=EVIDENCE_JSON, mode="json_object")

    monkeypatch.setattr(svc, "VisionProvider", _CountingProvider)
    monkeypatch.setattr(svc, "resolve_vision_config", lambda: _FakeCfg())
    svc.VisionBridge._instance = None

    state = _state(vision_evidence_text="<image-evidence>预先转写好的</image-evidence>")
    asyncio.run(FileContextMiddleware._inject_vision_evidence(state, IMAGE_FILES, "u1"))
    svc.VisionBridge._instance = None

    assert calls["n"] == 0, "已有预转写结果却又打了一次视觉模型"
    assert len(state.context) == 1
    assert "预先转写好的" in state.context[0].content[0].text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
