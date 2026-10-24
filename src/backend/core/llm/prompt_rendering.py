"""Render the active prompt version, or the file defaults, into text."""
from __future__ import annotations
from core.llm import prompt_versions
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
import logging
from core.llm import prompt_files
from core.llm import prompt_kinds
from core.llm import prompt_versions_state

logger = logging.getLogger(__name__)

def render_active_prompt(kind: str, db: Optional[Session] = None) -> Optional[str]:
    """Concatenate enabled parts of the active version into a single string.

    Returns None if no active version exists in DB (caller should fall back).
    """
    v = prompt_versions.get_active_version(kind, db=db)
    if not v:
        return None
    parts = v.get("parts") or []
    chunks: List[str] = []
    for p in parts:
        if not p.get("is_enabled", True):
            continue
        content = (p.get("content") or "").strip()
        if content:
            chunks.append(content)
    return "\n\n".join(chunks) if chunks else None


def render_active_prompt_part(
    kind: str, part_id: str, db: Optional[Session] = None
) -> Optional[str]:
    """Return a single named part of the active version (independent prompts).

    Used by distillation and platform-subagent prompts where each part is a standalone
    system prompt (skill_distiller / session_digest / colleague_distiller /
    personal_distiller) rather than a concatenation segment.

    Back-compat: an active version that predates the multi-part layout holds
    exactly one part (the legacy skill_distiller prompt) — only a request for
    ``skill_distiller`` may fall back to it; other part_ids must not silently
    receive the wrong prompt.
    Returns None if no active version / no match (caller falls back to fs).
    """
    v = prompt_versions.get_active_version(kind, db=db)
    if not v:
        return None
    parts = v.get("parts") or []
    for p in parts:
        if p.get("part_id") == part_id:
            content = (p.get("content") or "").strip()
            return content or None
    if len(parts) == 1 and part_id == "skill_distiller":
        content = (parts[0].get("content") or "").strip()
        return content or None
    return None


def skipped_parts(kind: str) -> set:
    try:
        from core.config.local_mode import local_mode_enabled

        if local_mode_enabled():
            from prompts.desktop_workspace import SKIP_PARTS
            return SKIP_PARTS.get(kind) or set()
    except Exception:
        logger.debug("local-mode check failed while assembling %s", kind, exc_info=True)
    return set()


def render_kind_segment(
    kind: str, db: Optional[Session] = None, *, fs_fallback: bool = True
) -> str:
    """某个 kind 装配好的正文：DB 激活版本 → 文件系统默认，都没有就返回空串。

    这是「一个 kind 的正文怎么取」的唯一实现——代码执行段、极速模式、任务计划清单
    以及 Config 管理台 ``/v1/admin/prompts/preview`` 的预览都走它，预览与智能体
    实际看到的因此不会漂移。

    文件系统这一步读的就是播种用的那批 md，所以尚未播种的部署渲染出来的，与它之后
    存进库里的是同一份。``fs_fallback=False`` 用于「对话模式」绑定的 kind：绑了个
    空 kind 就该退回默认装配，而不是套用别的 kind 的话术。
    """
    skip = skipped_parts(kind)
    try:
        rendered = render_active_parts(kind, db=db, skip=skip)
        if rendered:
            return rendered
    except Exception:
        logger.debug("render %s active prompt failed", kind, exc_info=True)
    if not fs_fallback or kind not in prompt_kinds.KIND_SPECS:
        return ""
    chunks = [
        (p.get("content") or "").strip()
        for p in prompt_files.read_fs_parts(kind)
        if p.get("is_enabled", True)
        and (p.get("content") or "").strip()
        and (p.get("part_id") or "") not in skip
    ]
    return "\n\n".join(chunks)


def render_active_parts(kind: str, *, db: Optional[Session], skip: set) -> Optional[str]:
    """激活版本的正文，按 ``skip`` 剔除若干段。``skip`` 为空时等同 render_active_prompt。"""
    if not skip:
        return render_active_prompt(kind, db=db)
    version = prompt_versions.get_active_version(kind, db=db)
    if not version:
        return None
    chunks = [
        (p.get("content") or "").strip()
        for p in (version.get("parts") or [])
        if p.get("is_enabled", True)
        and (p.get("content") or "").strip()
        and (p.get("part_id") or "").strip() not in skip
    ]
    return "\n\n".join(chunks) or None


def render_turbo_system_prompt(db: Optional[Session] = None) -> str:
    """极速模式（快速查询）的整份系统提示词。

    极速模式不做常规装配：智能体只带检索类工具，默认提示词的工具/流程段都不适用。
    """
    return render_kind_segment("turbo", db=db) or prompt_versions_state.TURBO_LAST_RESORT


def effective_parts(kind: str, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    """Read the active version, with whole-version filesystem fallback only."""
    try:
        version = prompt_versions.get_active_version(kind, db=db)
    except Exception:
        logger.warning("Prompt version unavailable for %s; using filesystem", kind, exc_info=True)
        version = None
    return version.get("parts", []) if version is not None else prompt_files.read_fs_parts(kind)
