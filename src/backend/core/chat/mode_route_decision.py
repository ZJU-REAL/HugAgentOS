"""智能路由的识别指令、模型输出解析与路由结论。

与 :mod:`core.chat.mode_router` 一起构成领域规则；这里只做纯函数（除读取提示词外不碰库）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from core.chat import mode_types
from core.chat.mode_router import (
    CAUSE_INVALID_SLUG,
    CAUSE_PARSE_ERROR,
    PROMPT_KIND,
    SOURCE_FALLBACK,
    SOURCE_KEPT,
    SOURCE_LLM,
    RoutePlan,
)
from core.config.store import intent_router_options as router_options
# A recognition request's raw result is the shared side-call outcome.
from core.llm.runtime.one_shot import ModelOutcome  # noqa: F401

logger = logging.getLogger(__name__)


# ── 识别指令与输出解析 ────────────────────────────────────────────────────


def _strategy_text(db: Optional[Session]) -> str:
    from core.llm import prompt_rendering

    try:
        return prompt_rendering.render_kind_segment(PROMPT_KIND, db=db).strip()
    except Exception:  # noqa: BLE001
        logger.debug("[intent_router] 读取识别策略提示词失败", exc_info=True)
        return ""


def build_instruction(plan: RoutePlan, *, strategy_text: Optional[str] = None) -> str:
    """追加在主请求末尾的识别指令：策略正文 + 候选清单 + 当前模式 + 只输出 JSON。"""
    text = strategy_text if strategy_text is not None else _strategy_text(None)
    allowed = plan.allowed()
    lines = ["<intent_routing>"]
    if text:
        lines += [text, ""]
    lines.append("## 候选模式")
    for item in allowed:
        scope = "检索速答（收窄工具）" if item.restricted else "能力完整（全部工具）"
        desc = f"：{item.description}" if item.description else ""
        lines.append(f"- `{item.slug}`「{item.name}」[{scope}]{desc}")
    lines.append("")
    current = plan.candidate(plan.current_slug)
    if current is not None:
        lines.append(f"## 当前模式\n`{current.slug}`「{current.name}」")
        if plan.strategy == router_options.STRATEGY_SMART_KEEP:
            lines.append("当前模式能完成就保持不变；只有它完成不了时才换成上面清单里的其他模式。")
    else:
        lines.append("## 当前模式\n没有当前模式（本段对话首次识别，或上一轮不是智能路由选的模式）。")
    lines += [
        "",
        "不要回答用户的问题，不要调用任何工具。只输出一个 JSON 对象："
        '{"mode_slug": "<上面清单中的 slug>", "reason": "<一句话理由>", "confidence": <0-1>}',
        "</intent_routing>",
    ]
    return "\n".join(lines)


def build_instruction_for(plan: RoutePlan, db: Optional[Session]) -> str:
    return build_instruction(plan, strategy_text=_strategy_text(db))


class DecisionParseError(ValueError):
    pass


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def parse_decision(text: str) -> Dict[str, Any]:
    """宽松解析模型输出：去掉思考块与代码围栏，取第一个 JSON 对象。"""
    raw = _THINK.sub("", str(text or "")).strip()
    raw = _FENCE.sub("", raw).strip()
    payload: Any = None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        start = raw.find("{")
        while start >= 0 and payload is None:
            depth = 0
            for idx in range(start, len(raw)):
                if raw[idx] == "{":
                    depth += 1
                elif raw[idx] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            payload = json.loads(raw[start : idx + 1])
                        except ValueError:
                            payload = None
                        break
            if payload is None:
                start = raw.find("{", start + 1)
    if not isinstance(payload, dict):
        raise DecisionParseError("model output is not a JSON object")
    slug = payload.get("mode_slug") or payload.get("slug") or payload.get("mode")
    if not isinstance(slug, str) or not slug.strip():
        raise DecisionParseError("mode_slug missing")
    confidence = payload.get("confidence")
    try:
        confidence = None if confidence is None else min(1.0, max(0.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = None
    return {
        "mode_slug": slug.strip().strip("`"),
        "reason": str(payload.get("reason") or "").strip()[:200],
        "confidence": confidence,
    }


# ── 结果 ──────────────────────────────────────────────────────────────────


def _result(plan: RoutePlan, slug: str, *, source: str, reason: str, confidence, latency_ms: int,
            cause: Optional[str]) -> Dict[str, Any]:
    picked = plan.candidate(slug)
    return {
        "mode_slug": slug,
        "mode_name": picked.name if picked else _builtin_name(slug),
        "reason": reason,
        "confidence": confidence,
        "source": source,
        "switched": bool(plan.current_slug) and plan.current_slug != slug,
        "previous_mode_slug": plan.current_slug,
        "candidates": [{"slug": c.slug, "name": c.name} for c in plan.candidates],
        "latency_ms": int(latency_ms),
        "fallback_cause": cause,
    }


def _builtin_name(slug: str) -> str:
    return {mode_types.SLUG_STANDARD: "标准模式", mode_types.SLUG_TURBO: "极速模式"}.get(slug, slug)


def decided_result(plan: RoutePlan, *, latency_ms: int = 0) -> Dict[str, Any]:
    assert plan.decided_slug is not None
    return _result(
        plan,
        plan.decided_slug,
        source=plan.decided_source or SOURCE_FALLBACK,
        reason=plan.decided_reason,
        confidence=None,
        latency_ms=latency_ms,
        cause=plan.decided_cause,
    )


def fallback_result(plan: RoutePlan, cause: str, *, latency_ms: int = 0, reason: str = "") -> Dict[str, Any]:
    return _result(
        plan, plan.fallback_slug, source=SOURCE_FALLBACK, reason=reason or "识别未成功，按回退模式继续",
        confidence=None, latency_ms=latency_ms, cause=cause,
    )


def resolve_outcome(plan: RoutePlan, outcome: ModelOutcome) -> Dict[str, Any]:
    """模型结果 → 本轮路由结论（含智能保持的降级拦截）。"""
    if outcome.error:
        return fallback_result(plan, outcome.error, latency_ms=outcome.latency_ms)
    try:
        decision = parse_decision(outcome.text)
    except DecisionParseError:
        return fallback_result(plan, CAUSE_PARSE_ERROR, latency_ms=outcome.latency_ms)
    picked = plan.candidate(decision["mode_slug"])
    if picked is None:
        return fallback_result(plan, CAUSE_INVALID_SLUG, latency_ms=outcome.latency_ms)
    current = plan.candidate(plan.current_slug)
    if picked.slug == plan.current_slug:
        return _result(plan, picked.slug, source=SOURCE_KEPT, reason=decision["reason"],
                       confidence=decision["confidence"], latency_ms=outcome.latency_ms, cause=None)
    if (
        plan.strategy == router_options.STRATEGY_SMART_KEEP
        and current is not None
        and picked.rank < current.rank
    ):
        # 智能保持：不往低档模式降，降级结论按保持处理（保住前缀缓存）。
        return _result(plan, current.slug, source=SOURCE_KEPT,
                       reason=f"保持当前模式（未从「{current.name}」降到「{picked.name}」）",
                       confidence=decision["confidence"], latency_ms=outcome.latency_ms, cause=None)
    return _result(plan, picked.slug, source=SOURCE_LLM, reason=decision["reason"],
                   confidence=decision["confidence"], latency_ms=outcome.latency_ms, cause=None)
