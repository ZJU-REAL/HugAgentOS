"""注入点测试：上传的图片如何进到主模型的上下文里。

覆盖 ``FileContextMiddleware`` 的两条分支——原生多模态直通 vs 视觉桥转写——以及
视觉桥不可用时的降级提示，还有 ``view_image`` 工具与 ``read_tool`` 的图片分支。
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
        "summary": "一张发票截图。",
        "ocr": {"full_text": "发票号 INV-2026-0042 金额 ¥1,280.00", "lines": []},
        "layout": {"regions": [{"type": "table", "reading_order": 1, "text": "金额明细"}]},
        "semantics": {"scene": "票据", "entities": [], "relations": []},
        "visual": {"dominant_colors": [], "notes": []},
        "uncertainty": [],
    },
    ensure_ascii=False,
)

IMAGE_FILES = [{"file_id": "art-1", "name": "invoice.png", "mime_type": "image/png"}]


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


def _stub_state():
    return SimpleNamespace(context=[], user_id="u1", model_provider_id="")


@pytest.fixture(autouse=True)
def _no_evidence_cache(monkeypatch):
    """本文件验证的是注入行为，不是缓存。

    证据缓存落 Redis、TTL 七天、key 只认 sha256(图)+模型+focus，所以同一张测试图会
    跨测试文件互相串结果（缓存本身的行为由 test_vision_bridge_e2e 覆盖）。这里整体
    关掉，让每个用例都真的走一次识别。
    """
    from core.vision.service import VisionBridge

    async def _miss(_key):
        return None

    async def _skip(_key, _result):
        return None

    monkeypatch.setattr(VisionBridge, "_cache_get", staticmethod(_miss))
    monkeypatch.setattr(VisionBridge, "_cache_set", staticmethod(_skip))


@pytest.fixture()
def working_bridge(monkeypatch):
    """让视觉桥可用，并总是返回一份固定证据。"""
    from core.vision import service as svc
    from core.vision.provider import VisionCallResult

    class _FakeProvider:
        def __init__(self, cfg):
            pass

        async def describe(self, *a, **kw):
            return VisionCallResult(text=EVIDENCE_JSON, mode="json_object")

    monkeypatch.setattr(svc, "VisionProvider", _FakeProvider)
    monkeypatch.setattr(svc, "resolve_vision_config", lambda: _FakeCfg())
    svc.VisionBridge._instance = None
    yield
    svc.VisionBridge._instance = None


@pytest.fixture()
def artifact_bytes(monkeypatch):
    # 附件字节的读取已收归 core.vision.attachments，它在调用时才 from core.llm.hooks
    # 导入，所以补丁要打在 hooks 上（打在 middlewares 上从此无效）。
    from core.llm import hooks

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: PNG_1X1)


# ── 主路径：纯文本模型 → 注入转写证据 ───────────────────────────────────────


def test_uploaded_image_becomes_text_evidence(working_bridge, artifact_bytes):
    from core.llm.middlewares import FileContextMiddleware

    st = _stub_state()
    asyncio.run(FileContextMiddleware._inject_vision_evidence(st, IMAGE_FILES, "u1"))

    assert len(st.context) == 1
    msg = st.context[0]
    assert msg.role == "user"
    text = msg.content[0].text
    # 上下文里拿到的是文字，不是 base64 图块
    assert "invoice.png" in text
    assert "INV-2026-0042" in text
    assert "<image-evidence" in text
    assert "不可信" in text
    assert "view_image" in text
    assert "base64" not in text


def test_bridge_unavailable_says_so_instead_of_silently_dropping(monkeypatch, artifact_bytes):
    from core.llm.middlewares import FileContextMiddleware
    from core.vision import service as svc

    monkeypatch.setattr(svc, "resolve_vision_config", lambda: None)
    st = _stub_state()
    asyncio.run(FileContextMiddleware._inject_vision_evidence(st, IMAGE_FILES, "u1"))

    text = st.context[0].content[0].text
    assert "不支持直接读图" in text
    assert "模型管理" in text, "降级提示必须告诉用户去哪儿配置"


def test_all_reads_failing_is_reported(monkeypatch, working_bridge):
    """桥配了但每张图都识别失败时要明说，不能让模型当没收到图。"""
    from core.llm import hooks
    from core.llm.middlewares import FileContextMiddleware
    from core.vision import service as svc

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: PNG_1X1)

    class _DeadProvider:
        def __init__(self, cfg):
            pass

        async def describe(self, *a, **kw):
            from core.vision.provider import VisionCallResult

            return VisionCallResult(error="boom")

    monkeypatch.setattr(svc, "VisionProvider", _DeadProvider)
    svc.VisionBridge._instance = None

    st = _stub_state()
    asyncio.run(FileContextMiddleware._inject_vision_evidence(st, IMAGE_FILES, "u1"))
    text = st.context[0].content[0].text
    assert "视觉模型调用失败" in text


def test_unreadable_attachment_injects_nothing(monkeypatch, working_bridge):
    from core.llm import hooks
    from core.llm.middlewares import FileContextMiddleware

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: None)
    st = _stub_state()
    asyncio.run(FileContextMiddleware._inject_vision_evidence(st, IMAGE_FILES, "u1"))
    assert st.context == []


# ── 分支选择：原生多模态 vs 走桥 ────────────────────────────────────────────


def test_effective_capability_prefers_per_request_provider(monkeypatch):
    from core.llm.middlewares import _effective_model_supports_vision
    from core.services.model_config import ModelConfigService

    class _Service:
        @staticmethod
        def resolve(role):
            return SimpleNamespace(extra={"supports_vision": False})

        @staticmethod
        def resolve_provider(pid):
            return SimpleNamespace(extra={"supports_vision": True}) if pid == "vl" else None

    monkeypatch.setattr(ModelConfigService, "get_instance", staticmethod(lambda: _Service()))

    # 用户本轮切到了一个多模态模型 → 图片直通，不走桥
    assert _effective_model_supports_vision(SimpleNamespace(model_provider_id="vl")) is True
    # 没切模型 → 看 main_agent，它是纯文本 → 走桥
    assert _effective_model_supports_vision(SimpleNamespace(model_provider_id="")) is False


def test_capability_probe_failure_falls_back_to_bridge(monkeypatch):
    """探测能力位本身出错时，按「看不见」处理——宁可多转写一次，不可让图静默消失。"""
    from core.llm.middlewares import _effective_model_supports_vision
    from core.services.model_config import ModelConfigService

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ModelConfigService, "get_instance", staticmethod(_boom))
    assert _effective_model_supports_vision(SimpleNamespace(model_provider_id="")) is False


# ── read_tool 的图片分支 ────────────────────────────────────────────────────


def test_read_tool_returns_evidence_for_images(working_bridge):
    from core.llm.tools.read_tool import _read_image_as_evidence

    payload = asyncio.run(_read_image_as_evidence(PNG_1X1, "/workspace/shot.png"))
    assert payload is not None
    assert payload["type"] == "image_evidence"
    assert "INV-2026-0042" in payload["evidence"]
    assert "view_image" in payload["hint"]


def test_read_tool_leaves_non_images_alone(working_bridge):
    """非图片二进制必须回落到原有的 type=binary 处理，不能被视觉桥截胡。"""
    from core.llm.tools.read_tool import _read_image_as_evidence

    assert asyncio.run(_read_image_as_evidence(b"PK\x03\x04zipdata", "/workspace/a.docx")) is None


def test_read_tool_falls_through_when_vision_unconfigured(monkeypatch):
    from core.llm.tools.read_tool import _read_image_as_evidence
    from core.vision import service as svc

    monkeypatch.setattr(svc, "resolve_vision_config", lambda: None)
    assert asyncio.run(_read_image_as_evidence(PNG_1X1, "/workspace/shot.png")) is None


# ── view_image 工具 ────────────────────────────────────────────────────────


def _register_view_image(working=True):
    """把 view_image 注册到一个最小 toolkit 上并取回其可调用体。"""
    from core.llm.tools.view_image_tool import register_view_image

    captured = {}

    class _Toolkit:
        @staticmethod
        def register_tool_function(fn, **kw):
            captured["fn"] = fn

    register_view_image(_Toolkit(), chat_id="c1", user_id="u1")
    return captured["fn"]


def _tool_json(response):
    return json.loads(response.content[0].text)


def test_view_image_reads_by_file_id(working_bridge, monkeypatch):
    from core.llm import hooks

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: PNG_1X1)
    fn = _register_view_image()
    payload = _tool_json(asyncio.run(fn(file_id="art-1", focus="发票号是多少")))
    assert payload["type"] == "image_evidence"
    assert payload["focus"] == "发票号是多少"
    assert "INV-2026-0042" in payload["evidence"]


def test_view_image_requires_a_target(working_bridge):
    fn = _register_view_image()
    payload = _tool_json(asyncio.run(fn()))
    assert "file_path 或 file_id" in payload["error"]


def test_view_image_rejects_non_image_bytes(working_bridge, monkeypatch):
    from core.llm import hooks

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: b"PK\x03\x04zip")
    fn = _register_view_image()
    payload = _tool_json(asyncio.run(fn(file_id="art-1")))
    assert "不是可识别的图片格式" in payload["error"]


def test_view_image_reports_missing_vision_config(monkeypatch):
    from core.llm import hooks
    from core.vision import service as svc

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: PNG_1X1)
    monkeypatch.setattr(svc, "resolve_vision_config", lambda: None)
    fn = _register_view_image()
    payload = _tool_json(asyncio.run(fn(file_id="art-1")))
    assert "模型管理" in payload["error"]


def test_view_image_not_registered_for_native_multimodal_model(monkeypatch):
    """主模型自己能看图时不注册该工具——经二道手只会掉精度。"""
    from core.llm import agent_factory
    from core.services.model_config import ModelConfigService

    class _Service:
        @staticmethod
        def resolve(role):
            return SimpleNamespace(extra={"supports_vision": True})

    monkeypatch.setattr(ModelConfigService, "get_instance", staticmethod(lambda: _Service()))
    assert agent_factory._vision_bridge_needed() is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
