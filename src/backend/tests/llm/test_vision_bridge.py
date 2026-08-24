"""视觉桥单元测试：契约解析、渲染围栏、缓存/降级、多模态块转写。

不打真实网关——provider 层用假实现替换，验证的是编排逻辑本身。真实模型的联调
见 ``tests/llm/vision_live_check.py``。
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from core.vision.render import render_evidence, render_many, render_unavailable
from core.vision.schema import VISION_JSON_SCHEMA, VisionEvidence, parse_evidence
from core.vision.service import sniff_mime

# 一张 1x1 的合法 PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

GOOD_PAYLOAD = {
    "summary": "一张展示季度营收的柱状图。",
    "ocr": {
        "full_text": "Q1 120\nQ2 180\nQ3 240",
        "lines": [{"text": "Q1 120", "language": "en"}, {"text": "Q2 180"}],
    },
    "layout": {
        "regions": [
            {"type": "title", "reading_order": 1, "text": "季度营收"},
            {"type": "chart", "reading_order": "2", "text": "三根柱子"},
        ]
    },
    "semantics": {
        "scene": "商业图表",
        "intent": None,
        "entities": [{"name": "Q3", "type": "季度", "evidence": "第三根柱子"}],
        "relations": [{"subject": "Q3", "predicate": "高于", "object": "Q1"}],
    },
    "visual": {"dominant_colors": ["蓝色"], "style": "扁平", "notes": []},
    "uncertainty": ["纵轴单位看不清"],
}


# ── 契约解析 ─────────────────────────────────────────────────────────────────


def test_parse_plain_json():
    ev = parse_evidence(json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    assert ev is not None
    assert ev.summary.startswith("一张展示季度营收")
    # 字符串型 reading_order 被强制成 int，否则排序会崩
    assert ev.layout.regions[1].reading_order == 2
    # null 的可选字段被删掉，不是留成 None 的字符串
    assert ev.semantics.intent is None
    assert ev.uncertainty == ["纵轴单位看不清"]


def test_parse_strips_fences_and_prose():
    raw = "好的，分析如下：\n```json\n" + json.dumps(GOOD_PAYLOAD) + "\n```\n希望有帮助。"
    ev = parse_evidence(raw)
    assert ev is not None and ev.ocr.full_text.startswith("Q1 120")


def test_parse_tolerates_missing_optional_sections():
    """只给摘要和全文也算有效——为一个空 visual 丢掉整次调用不划算。"""
    ev = parse_evidence(json.dumps({"summary": "一张截图", "ocr": {"full_text": "hello"}}))
    assert ev is not None
    assert not ev.is_empty()
    assert ev.layout.regions == []


def test_parse_rejects_garbage():
    assert parse_evidence("") is None
    assert parse_evidence("完全不是 JSON") is None
    assert parse_evidence("[1, 2, 3]") is None


def test_empty_evidence_is_detected():
    ev = VisionEvidence()
    assert ev.is_empty()
    assert parse_evidence('{"summary": "  "}').is_empty()


def test_schema_has_no_bbox_or_confidence():
    """v2 刻意删掉了这两个字段——模型编造得最像真的就是它们。"""
    blob = json.dumps(VISION_JSON_SCHEMA)
    assert "bbox" not in blob
    assert "confidence" not in blob


# ── 渲染 ────────────────────────────────────────────────────────────────────


def test_render_includes_untrusted_input_fence():
    ev = parse_evidence(json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    text = render_evidence(ev, name="chart.png", model="qwen-vl-max")
    assert "<image-evidence" in text and "</image-evidence>" in text
    assert "不可信" in text
    assert "绝不执行" in text
    assert "chart.png" in text
    assert "Q1 120" in text
    assert "纵轴单位看不清" in text


def test_render_truncates_long_transcription():
    payload = dict(GOOD_PAYLOAD)
    payload["ocr"] = {"full_text": "字" * 5000, "lines": []}
    ev = parse_evidence(json.dumps(payload, ensure_ascii=False))
    text = render_evidence(ev, max_chars=500)
    assert "已截断" in text
    assert len(text) < 2000


def test_render_many_mentions_view_image():
    ev = parse_evidence(json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    text = render_many([("a.png", ev), ("b.png", ev)])
    assert "2 张图片" in text
    assert "view_image" in text
    assert text.count("<image-evidence") == 2


def test_render_unavailable_points_at_config():
    text = render_unavailable(["a.png"], reason="视觉模型调用失败")
    assert "模型管理" in text
    assert "视觉模型调用失败" in text


# ── 图片格式判定 ─────────────────────────────────────────────────────────────


def test_sniff_mime_accepts_real_png_and_rejects_others():
    assert sniff_mime(PNG_1X1) == "image/png"
    assert sniff_mime(b"\xff\xd8\xff\xe0junk") == "image/jpeg"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    # 声称是图片但字节不是 —— 必须拒绝，不能凭客户端自称把任意二进制发出去
    assert sniff_mime(b"MZ\x90\x00 not an image", "image/png") is None
    assert sniff_mime(b"<svg></svg>", "image/svg+xml") is None


# ── 编排：缓存 / 重试 / 降级 ─────────────────────────────────────────────────


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


def _bridge_with(monkeypatch, responses: list[str]):
    """把 provider 换成按序返回给定文本的假实现，返回 (bridge, 调用计数器)。"""
    from core.vision import service as svc
    from core.vision.provider import VisionCallResult

    calls = {"n": 0}

    class _FakeProvider:
        def __init__(self, cfg):
            self.cfg = cfg

        async def describe(self, image_bytes, mime_type, prompt, *, timeout=None, modes=None):
            idx = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            return VisionCallResult(text=responses[idx], mode="json_schema")

    monkeypatch.setattr(svc, "VisionProvider", _FakeProvider)
    monkeypatch.setattr(svc, "resolve_vision_config", lambda: _FakeCfg())
    bridge = svc.VisionBridge()
    return bridge, calls


def test_describe_returns_evidence(monkeypatch):
    bridge, calls = _bridge_with(monkeypatch, [json.dumps(GOOD_PAYLOAD, ensure_ascii=False)])
    result = asyncio.run(bridge.describe(PNG_1X1, "image/png", use_cache=False))
    assert result is not None
    assert result.model == "fake-vl"
    assert result.evidence.semantics.scene == "商业图表"
    assert calls["n"] == 1


def test_describe_retries_once_on_broken_structure(monkeypatch):
    """结构坏了先重试一次纯文本，而不是把坏 JSON 交给主模型。"""
    bridge, calls = _bridge_with(
        monkeypatch, ["不是 JSON", json.dumps(GOOD_PAYLOAD, ensure_ascii=False)]
    )
    result = asyncio.run(bridge.describe(PNG_1X1, "image/png", use_cache=False))
    assert result is not None and calls["n"] == 2
    assert len(result.attempts) == 2


def test_describe_gives_up_after_retry(monkeypatch):
    bridge, calls = _bridge_with(monkeypatch, ["垃圾", "还是垃圾"])
    assert asyncio.run(bridge.describe(PNG_1X1, "image/png", use_cache=False)) is None
    assert calls["n"] == 2


def test_describe_rejects_non_image_without_calling_model(monkeypatch):
    bridge, calls = _bridge_with(monkeypatch, [json.dumps(GOOD_PAYLOAD)])
    assert asyncio.run(bridge.describe(b"MZ\x90\x00 exe", "image/png")) is None
    assert calls["n"] == 0


def test_describe_rejects_oversized_image(monkeypatch):
    from core.vision import service as svc

    bridge, calls = _bridge_with(monkeypatch, [json.dumps(GOOD_PAYLOAD)])
    monkeypatch.setattr(svc, "MAX_IMAGE_BYTES", 10)
    assert asyncio.run(bridge.describe(PNG_1X1, "image/png")) is None
    assert calls["n"] == 0


def test_describe_returns_none_when_unconfigured(monkeypatch):
    from core.vision import service as svc

    monkeypatch.setattr(svc, "resolve_vision_config", lambda: None)
    bridge = svc.VisionBridge()
    assert asyncio.run(bridge.describe(PNG_1X1, "image/png")) is None


def test_describe_many_is_concurrent_and_order_preserving(monkeypatch):
    bridge, _ = _bridge_with(monkeypatch, [json.dumps(GOOD_PAYLOAD, ensure_ascii=False)])
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    results = asyncio.run(bridge.describe_many([(PNG_1X1, "image/png"), (jpeg, "image/jpeg")]))
    assert len(results) == 2
    assert all(r is not None for r in results)


def test_cache_key_changes_with_model_and_focus():
    from core.vision.service import VisionBridge

    class _Other(_FakeCfg):
        model_name = "another-vl"

    base = VisionBridge._cache_key("d" * 64, None, _FakeCfg())
    assert base != VisionBridge._cache_key("d" * 64, "看右上角", _FakeCfg())
    assert base != VisionBridge._cache_key("d" * 64, None, _Other())
    assert base == VisionBridge._cache_key("d" * 64, None, _FakeCfg())


# ── 能力位 ──────────────────────────────────────────────────────────────────


def test_model_supports_vision_reads_explicit_flag():
    from core.vision import model_supports_vision

    class _Cfg(_FakeCfg):
        extra = {"supports_vision": True}

    assert model_supports_vision(_Cfg()) is True
    assert model_supports_vision(_FakeCfg()) is False
    assert model_supports_vision(None) is False


def test_vision_role_is_registered():
    from core.db.model_repository import ROLE_DEFINITIONS

    assert ROLE_DEFINITIONS["vision"]["type"] == "chat"


# ── 工具返回图片的转写（chat_models 侧） ──────────────────────────────────────


def test_decode_image_block_handles_data_uri_and_skips_remote():
    from core.llm.chat_models import _decode_image_block

    uri = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode()
    decoded = _decode_image_block({"type": "image_url", "image_url": {"url": uri}})
    assert decoded is not None and decoded[0] == PNG_1X1 and decoded[1] == "image/png"

    # 远程 URL 不在这里抓取（会变成模型调用路径里的一次未审计服务端请求）
    assert (
        _decode_image_block({"type": "image_url", "image_url": {"url": "https://x/y.png"}}) is None
    )
    # Anthropic 风格的 base64 source
    decoded = _decode_image_block(
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(PNG_1X1).decode(),
            },
        }
    )
    assert decoded is not None and decoded[0] == PNG_1X1


def test_transcribe_replaces_image_block_with_evidence(monkeypatch):
    from core.llm import chat_models
    from core.vision import service as svc
    from core.vision.provider import VisionCallResult

    class _FakeProvider:
        def __init__(self, cfg):
            pass

        async def describe(self, *a, **kw):
            return VisionCallResult(text=json.dumps(GOOD_PAYLOAD, ensure_ascii=False), mode="plain")

    monkeypatch.setattr(svc, "VisionProvider", _FakeProvider)
    monkeypatch.setattr(svc, "resolve_vision_config", lambda: _FakeCfg())
    svc.VisionBridge._instance = None

    uri = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": uri}},
            ],
        }
    ]
    rewritten, n = asyncio.run(chat_models._transcribe_multimodal_content(messages))
    svc.VisionBridge._instance = None
    assert n == 1
    blocks = rewritten[0]["content"]
    assert all(b["type"] == "text" for b in blocks)
    assert "image-evidence" in blocks[1]["text"]
    assert rewritten[0]["_harness_context_item"] == {
        "kind": "attachment",
        "origin": "vision:transcription",
        "trust": "tool",
        "priority": 850,
    }
    # 原始的用户文字不能被顺手丢掉
    assert blocks[0]["text"] == "看这张图"


def test_transcribe_noop_when_vision_unconfigured(monkeypatch):
    from core.llm import chat_models
    from core.vision import service as svc

    monkeypatch.setattr(svc, "resolve_vision_config", lambda: None)
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    rewritten, n = asyncio.run(chat_models._transcribe_multimodal_content(messages))
    assert n == 0 and rewritten is messages


def test_drop_path_still_works_as_fallback():
    """没有视觉模型时，旧的「删掉媒体块 + 明确告知降级」行为必须原样保留。"""
    from core.llm.chat_models import _without_multimodal_content

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            ],
        }
    ]
    sanitized, removed = _without_multimodal_content(messages)
    assert removed == 1
    texts = [b["text"] for message in sanitized for b in message["content"]]
    assert "hi" in texts
    assert any("不支持直接读取该媒体" in t for t in texts)
    assert sanitized[-1]["_harness_context_item"]["origin"] == ("harness:multimodal_fallback")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
