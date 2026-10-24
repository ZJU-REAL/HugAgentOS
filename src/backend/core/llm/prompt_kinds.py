"""Prompt kinds: built-in specs plus admin-defined kinds."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from extensions.paths import DEFAULT_AGENTS_DIR
from sqlalchemy.orm import Session
from core.llm import prompt_storage

@dataclass(frozen=True)
class KindSpec:
    """一个内置 kind 的全部差异都集中在这里，别处不再按 kind 分支。

    ``layout`` 决定目录里的 md 文件怎么变成 parts：

    - ``concat``：目录下每个 ``*.system.md`` 是一段，拼起来构成一份提示词
    - ``independent``：每个文件是一份互不相干的独立提示词，按 part_id 单取
    - ``single``：整个 kind 只有 ``<kind>.system.md`` 一个文件
    """

    subdir: tuple[str, ...]
    layout: str
    label: str
    name: str
    desc: str
    visible: bool = True
    preview: str | None = None
    #: 兜底 md 的绝对目录。可选模块的提示词随模块代码放（不进 CE），不在 prompt_text/ 下。
    root: Path | None = None


def register_kind(key: str, spec: KindSpec) -> None:
    """可选模块注册自己的内置 kind（模块关闭时不注册，Config 台就不出现这个 tab）。

    须在启动播种（``seed_from_filesystem``）之前调用，即放在模块的 ``setup()`` 里。
    """
    KIND_SPECS[key] = spec


def list_custom_kinds(db: Optional[Session] = None) -> List[Dict[str, str]]:
    """管理员自建的 kind（``[{"key","label"}]``）。

    存在版本池同一个 ContentBlock 的 ``custom_kinds`` 下——它们和内置 kind 共用
    versions/active 两张表，差别只在于没有文件系统兜底、可以删。
    「对话模式」就是靠这个扩展的：管理员在提示词管理新开一个 tab，模式那边就能绑它。
    """
    payload = prompt_storage.load_payload(db)
    out: List[Dict[str, str]] = []
    for item in payload.get("custom_kinds") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key and key not in KIND_SPECS:
            out.append({"key": key, "label": str(item.get("label") or key)})
    return out


def all_kinds(db: Optional[Session] = None) -> List[Dict[str, str]]:
    """内置 + 自定义的完整 kind 清单，供管理端渲染 tab 与模式绑定下拉。"""
    items = [{"key": k, "label": s.label, "builtin": True, "visible": s.visible, "preview": s.preview, "description": s.desc} for k, s in KIND_SPECS.items()]
    items += [{**c, "builtin": False} for c in list_custom_kinds(db)]
    return items


def is_valid_kind(kind: str, db: Optional[Session] = None) -> bool:
    if kind in KIND_SPECS:
        return True
    return any(c["key"] == kind for c in list_custom_kinds(db))


def create_custom_kind(
    key: str, label: str, db: Optional[Session] = None, updated_by: str = "admin"
) -> Dict[str, str]:
    """新开一个 kind（tab），并给它建一个空的 ``default`` 版本并激活。

    没有初始版本的 kind 在管理端会是个点进去什么都没有的空 tab，所以这里顺手建上，
    管理员进去直接就能写正文。
    """
    cleaned = "".join(
        ch if (ch.isalnum() and ch.isascii()) or ch in "-_" else "_" for ch in (key or "").strip().lower()
    ).strip("-_")
    if not cleaned:
        raise ValueError("kind key required")
    if cleaned in KIND_SPECS:
        raise ValueError(f"「{cleaned}」是内置 kind，不能重复创建")
    payload = prompt_storage.clone(prompt_storage.load_payload(db))
    customs = payload.setdefault("custom_kinds", [])
    if any(str((c or {}).get("key")) == cleaned for c in customs):
        raise ValueError(f"kind「{cleaned}」已存在")
    customs.append({"key": cleaned, "label": (label or cleaned).strip()[:60]})

    # 空的初始版本：一个可编辑的 part，内容留空由管理员填。
    versions = payload.setdefault("versions", [])
    now = datetime.now(timezone.utc).isoformat()
    versions.append(
        {
            "kind": cleaned,
            "id": "default",
            "name": (label or cleaned).strip()[:60],
            "description": "",
            "parts": [{"part_id": cleaned, "display_name": label or cleaned, "content": ""}],
            "created_at": now,
            "updated_at": now,
            "updated_by": updated_by,
        }
    )
    payload.setdefault("active", {})[cleaned] = "default"
    prompt_storage.save_payload(payload, db=db, updated_by=updated_by)
    return {"key": cleaned, "label": (label or cleaned).strip()[:60]}


def delete_custom_kind(key: str, db: Optional[Session] = None) -> bool:
    """删掉一个自定义 kind 及其全部版本。内置 kind 拒绝删除。

    注意：绑了这个 kind 的对话模式会退回默认提示词装配（``render_kind_segment(fs_fallback=False)``
    取不到就返回空串，装配侧按"没配提示词"处理），不会让对话起不来。
    """
    if key in KIND_SPECS:
        raise ValueError("内置 kind 不可删除")
    payload = prompt_storage.clone(prompt_storage.load_payload(db))
    customs = payload.get("custom_kinds") or []
    remaining = [c for c in customs if str((c or {}).get("key")) != key]
    if len(remaining) == len(customs):
        return False
    payload["custom_kinds"] = remaining
    payload["versions"] = [v for v in (payload.get("versions") or []) if v.get("kind") != key]
    (payload.get("active") or {}).pop(key, None)
    prompt_storage.save_payload(payload, db=db)
    return True


KIND_SPECS: Dict[str, KindSpec] = {
    "desktop": KindSpec(("desktop",), "independent", "桌面端", "default - 桌面端提示词", "本机环境、文件、项目与工具规则；仅桌面端装配", preview="desktop-preview"),
    "system": KindSpec(
        ("default", "system"),
        "concat",
        "系统提示词",
        "default - 标准系统提示词",
        "当前默认系统提示词，包含 role / constraints / tools / workflow / format",
    ),
    "turbo": KindSpec(
        ("turbo",),
        "single",
        "极速模式",
        "default - 极速模式",
        "极速模式（快速查询）系统提示词：仅联网搜索/网页抓取/知识库检索三类工具，"
        "1-2 轮并行调用后直接作答",
    ),
    "plan_mode": KindSpec(
        ("plan_mode",),
        "single",
        "计划模式",
        "default - 计划模式",
        "Plan 模式下用于拆解用户任务为可执行步骤的 sub-agent 系统提示词",
    ),
    "plan_tool": KindSpec(
        ("plan_tool",),
        "single",
        "任务计划清单",
        "default - 任务计划清单",
        "顶层对话注册 update_plan 工具时追加的计划清单说明",
    ),
    "code_exec": KindSpec(
        ("code_exec", "system"),
        "concat",
        "代码执行",
        "default - 代码执行 (沙盒)",
        "Lab 代码执行模式的系统提示词（沙盒环境、工具能力、执行规范等）",
    ),
    "distillation": KindSpec(
        ("distillation",),
        "independent",
        "蒸馏",
        "default - 技能蒸馏",
        "从对话轨迹蒸馏出可复用技能的系统提示词",
    ),
    "subagents": KindSpec(
        ("subagents",),
        "independent",
        "平台默认子智能体",
        "default - 平台默认子智能体",
        "探索员、执行员和审查员三个平台内置角色的独立系统提示词",
        visible=False,
        root=DEFAULT_AGENTS_DIR,
    ),
}


BUILTIN_KINDS = tuple(KIND_SPECS)


VALID_KINDS = BUILTIN_KINDS
