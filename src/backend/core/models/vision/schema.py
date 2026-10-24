"""视觉证据契约（Vision Evidence v2）。

纯文本主模型看不见图片，视觉桥把图片转成**结构化文字证据**再注入上下文。这份契约
对齐 liustack/modlens 的 v2 输出契约：六个顶层字段 summary / ocr / layout /
semantics / visual / uncertainty。

两处刻意的设计：

1. **不含 bbox 像素坐标，也不含 confidence 置信度分数**。视觉模型在这两个字段上
   编得最像真的，留着只会把幻觉伪装成结构化数据。读不出来的内容一律进
   ``uncertainty``，而不是脑补一个坐标。
2. **缺字段降级而非整单作废**。modlens 六个字段全必填（结构坏了就换 provider 重来），
   我们通常只配一个视觉模型，为一个空的 ``visual`` 丢掉整次调用不划算：这里除
   ``summary`` 外全部带默认值，只有「摘要、全文、版面区域**同时**为空」才判定这次
   识别没拿到任何东西（见 :meth:`VisionEvidence.is_empty`）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class OcrLine(BaseModel):
    text: str = ""
    language: Optional[str] = None


class Ocr(BaseModel):
    full_text: str = ""
    lines: list[OcrLine] = Field(default_factory=list)


class LayoutRegion(BaseModel):
    # 开放集合，不是封闭枚举：网页截图里的 link、门户页里的 search 都得放得下。
    # 常用词表只作为提示写进 JSON Schema 的 description，不做校验。
    type: str = "unknown"
    reading_order: int = 0
    text: str = ""

    @field_validator("reading_order", mode="before")
    @classmethod
    def _coerce_order(cls, v: Any) -> int:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


class Layout(BaseModel):
    regions: list[LayoutRegion] = Field(default_factory=list)


class Entity(BaseModel):
    name: str = ""
    type: str = ""
    evidence: Optional[str] = None


class Relation(BaseModel):
    subject: str = ""
    predicate: str = ""
    object: str = ""


class Semantics(BaseModel):
    scene: str = ""
    intent: Optional[str] = None
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


class Visual(BaseModel):
    dominant_colors: list[str] = Field(default_factory=list)
    style: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class VisionEvidence(BaseModel):
    summary: str = ""
    ocr: Ocr = Field(default_factory=Ocr)
    layout: Layout = Field(default_factory=Layout)
    semantics: Semantics = Field(default_factory=Semantics)
    visual: Visual = Field(default_factory=Visual)
    uncertainty: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """这次识别是否什么都没拿到（触发重试/降级的判据）。"""
        return not (
            self.summary.strip() or self.ocr.full_text.strip() or self.layout.regions
        )


# ── JSON Schema（喂给支持 response_format=json_schema 的网关） ────────────────

_REGION_TYPE_HINT = (
    "A short kind for this region. Prefer a common one where it fits: title, heading, "
    "paragraph, list, table, chart, form, code, image, icon, link, nav, button, search. "
    "Any other short label is fine when none of those describe it."
)

VISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "ocr": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "full_text": {"type": "string"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "text": {"type": "string"},
                            "language": {"type": "string"},
                        },
                        "required": ["text"],
                    },
                },
            },
            "required": ["full_text", "lines"],
        },
        "layout": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "regions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "type": {"type": "string", "description": _REGION_TYPE_HINT},
                            "reading_order": {"type": "number"},
                            "text": {"type": "string"},
                        },
                        "required": ["type", "reading_order", "text"],
                    },
                }
            },
            "required": ["regions"],
        },
        "semantics": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "scene": {"type": "string"},
                "intent": {"type": "string"},
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                            "evidence": {"type": "string"},
                        },
                        "required": ["name", "type"],
                    },
                },
                "relations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "subject": {"type": "string"},
                            "predicate": {"type": "string"},
                            "object": {"type": "string"},
                        },
                        "required": ["subject", "predicate", "object"],
                    },
                },
            },
            "required": ["scene", "entities"],
        },
        "visual": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "dominant_colors": {"type": "array", "items": {"type": "string"}},
                "style": {"type": "string"},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
        },
        "uncertainty": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "ocr", "layout", "semantics", "visual", "uncertainty"],
}


# ── 解析 ─────────────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def _extract_json_object(text: str) -> Optional[str]:
    """从自由文本里抠出第一个平衡的 JSON 对象（模型爱在 JSON 前后加寒暄）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _prune_nulls(value: Any) -> Any:
    """递归删掉值为 null 的键。

    模型在没话可说的可选字段上经常写 ``null``。删键而不是留 null，读取方只需判断
    「在不在」，不必再判断「是不是 None」。
    """
    if isinstance(value, dict):
        return {k: _prune_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_prune_nulls(v) for v in value if v is not None]
    return value


def parse_evidence(raw: str) -> Optional[VisionEvidence]:
    """把模型返回的原始文本解析成 :class:`VisionEvidence`；解析不出来返回 ``None``。"""
    if not raw or not raw.strip():
        return None
    candidate = _strip_fences(raw)
    payload: Any = None
    for text in (candidate, _extract_json_object(candidate) or ""):
        if not text:
            continue
        try:
            payload = json.loads(text)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(payload, dict):
        return None
    try:
        return VisionEvidence.model_validate(_prune_nulls(payload))
    except Exception:  # noqa: BLE001 — 结构不符即视作本次识别失败，由调用方决定重试
        return None
