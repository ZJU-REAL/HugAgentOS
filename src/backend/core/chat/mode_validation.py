"""Normalise and validate chat mode fields from request bodies."""
from __future__ import annotations
from core.infra.time import utc_now
import uuid
from typing import Any, Dict, List, Optional
from core.db.models.chat_mode import EFFORT_VALUES, TOOL_SCOPE_ALL, TOOL_SCOPE_RESTRICTED
from core.infra.exceptions import BadRequestError

def as_id_list(value: Any) -> List[str]:
    """JSON 列 → 干净的字符串 id 列表（去空白、去重、保序）。"""
    if not isinstance(value, (list, tuple)):
        return []
    seen: Dict[str, None] = {}
    for item in value:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip(), None)
    return list(seen.keys())


def writable_fields(
    payload: Dict[str, Any], owner_user_id: Optional[str], creating: bool
) -> Dict[str, Any]:
    """把请求体收敛成可写字段。未出现的键在更新时保持原值。"""
    out: Dict[str, Any] = {}

    def take(key: str, value: Any) -> None:
        if value is not None or creating:
            out[key] = value

    if creating or "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise BadRequestError("模式名称不能为空")
        out["name"] = name[:100]
    if creating or "description" in payload:
        out["description"] = str(payload.get("description") or "").strip()[:500]
    if creating or "enabled" in payload:
        out["enabled"] = bool(payload.get("enabled", True))
    if creating or "sort_order" in payload:
        try:
            out["sort_order"] = int(payload.get("sort_order", 100))
        except (TypeError, ValueError):
            out["sort_order"] = 100

    if creating or "tool_scope" in payload:
        scope = str(payload.get("tool_scope") or TOOL_SCOPE_RESTRICTED)
        out["tool_scope"] = scope if scope in (TOOL_SCOPE_ALL, TOOL_SCOPE_RESTRICTED) else TOOL_SCOPE_RESTRICTED
    for key in ("mcp_server_ids", "skill_ids", "plugin_ids", "agent_ids"):
        if creating or key in payload:
            out[key] = as_id_list(payload.get(key))
    if creating or "manual_invoke_enabled" in payload:
        out["manual_invoke_enabled"] = bool(payload.get("manual_invoke_enabled", True))
    if creating or "code_exec_enabled" in payload:
        out["code_exec_enabled"] = bool(payload.get("code_exec_enabled", False))
    if creating or "max_iters" in payload:
        raw = payload.get("max_iters")
        try:
            # 下限 2：配成 1 会把"回答那一轮"也掐掉，等于永远拿不到结果。
            out["max_iters"] = max(2, int(raw)) if raw not in (None, "") else None
        except (TypeError, ValueError):
            out["max_iters"] = None

    if creating or "default_effort" in payload:
        effort = str(payload.get("default_effort") or "fast")
        out["default_effort"] = effort if effort in EFFORT_VALUES else "fast"
    if creating or "effort_locked" in payload:
        out["effort_locked"] = bool(payload.get("effort_locked", False))

    # 提示词两种来源二选一：绑一个提示词管理里的 tab（prompt_kind，正文在那边
    # 编辑与版本化），或者就地手写一段正文（prompt_text）。互斥——同一行留两个
    # 来源，运行时不知道该听谁的。装配侧优先级 prompt_text > prompt_kind。
    #
    # 版本池的 tab 现在可以新增（prompt_version_service.create_custom_kind），
    # 所以"绑 tab"这条路对自建模式同样走得通，不再只有内置极速能用。
    _wants_kind = "prompt_kind" in payload
    _wants_text = "prompt_text" in payload
    if creating or _wants_kind or _wants_text:
        kind = str(payload.get("prompt_kind") or "").strip()
        if owner_user_id is not None:
            # 私有模式只许手写正文：分类是管理端的东西，绑上等于间接读管理员的
            # 提示词内容，越过了"用户看不到管理端提示词"的边界。
            kind = ""
        text = str(payload.get("prompt_text") or "").strip()
        if kind:
            out["prompt_kind"] = kind
            out["prompt_text"] = None
        elif text:
            out["prompt_text"] = text
            out["prompt_kind"] = None
        elif _wants_kind or _wants_text or creating:
            # 两个都清空 = 显式取消专属提示词，退回默认装配
            out["prompt_kind"] = None
            out["prompt_text"] = None
    take("updated_at", utc_now())
    return out


def normalize_slug(raw: Any, fallback_name: Any) -> str:
    """slug 归一：小写、只留 [a-z0-9_-]。空则由名称派生，再空则随机。"""
    candidate = str(raw or "").strip().lower()
    if not candidate:
        candidate = str(fallback_name or "").strip().lower()
    cleaned = "".join(ch if (ch.isalnum() and ch.isascii()) or ch in "-_" else "-" for ch in candidate)
    cleaned = cleaned.strip("-_")
    if not cleaned:
        cleaned = f"mode-{uuid.uuid4().hex[:8]}"
    return cleaned[:64]
