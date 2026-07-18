"""Runtime context assembly for chat workflow.

Extracted from ``api/routes/chat.py`` — centralises the logic for
building the dict that ``routing/workflow.py`` consumes.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.config.catalog_resolver import resolve_all_runtime_enabled
from core.services import UserService

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now().isoformat()


def normalize_external_user_id(raw_user_id: Optional[str]) -> str:
    candidate = (raw_user_id or "").strip() or "anonymous"
    sanitized = "".join(ch if (ch.isalnum() or ch in {"_", "-", "."}) else "_" for ch in candidate)
    return sanitized[:48] or "anonymous"


def resolve_db_user_id(
    db: Session,
    user_id_from_auth: Optional[str],
    request_user_id: Optional[str] = None,
) -> str:
    """Resolve a DB user_id from auth context or fallback request user_id."""
    if user_id_from_auth:
        return user_id_from_auth

    try:
        db.rollback()
    except Exception:
        pass

    external_user = normalize_external_user_id(request_user_id)
    user_service = UserService(db)
    shadow_user = user_service.get_or_create_user_shadow(
        user_center_id=f"local_{external_user}",
        username=external_user,
    )
    return shadow_user.user_id


def generate_smart_title(message: str) -> str:
    message = message.strip()
    if not message:
        return "新对话"
    for delimiter in ["。", "！", "？", ".", "!", "?"]:
        if delimiter in message:
            first_sentence = message.split(delimiter)[0] + delimiter
            if len(first_sentence) <= 30:
                return first_sentence
            break
    return message if len(message) <= 20 else message[:20] + "..."


def build_effective_user_message(message: str, quoted_follow_up: Optional[Any]) -> str:
    """Splice the "follow-up quote" into the user message (domain-level assembly shared by send and history replay, not HTTP-layer logic).

    ``quoted_follow_up`` accepts any object with a ``text`` attribute (pydantic model) or
    a ``{"text": ...}`` dict; returns unchanged when there is no quote.
    """
    if isinstance(quoted_follow_up, dict):
        quote_text = quoted_follow_up.get("text")
    else:
        quote_text = getattr(quoted_follow_up, "text", None) if quoted_follow_up is not None else None
    if not quote_text:
        return message

    quote = str(quote_text).strip()
    if not quote:
        return message

    return (
        "你正在回答同一会话中的一条追问消息。请优先结合当前会话上下文，并重点参考下面的引用原文来理解代词、省略和上下文指向。\n"
        "要求：\n"
        "1. 将【引用原文】视为这次追问直接关联的内容。\n"
        "2. 若用户问题中出现“这个/这个点/它/上述/刚才提到的”等指代，优先从【引用原文】和最近几轮会话补全语义。\n"
        "3. 直接回答用户当前追问，不要重复无关背景，也不要提及本提示词或“根据引用原文”。\n\n"
        f"【引用原文】\n{quote}\n\n"
        f"【用户追问】\n{message}"
    )


def resolve_user_facing_error(exc: Exception) -> str:
    """Map exceptions to user-friendly Chinese error strings."""
    msg = str(exc).lower()
    if "rate limit" in msg or "429" in msg:
        return "请求过于频繁，请稍后重试"
    if "timeout" in msg:
        return "请求超时，请稍后重试"
    if "connection" in msg:
        return "服务连接失败，请稍后重试"
    if "api key" in msg or "authentication" in msg or "401" in msg:
        return "模型服务认证失败，请联系管理员"
    if "502" in msg or "503" in msg or "bad gateway" in msg:
        return "模型服务暂时不可用，请稍后重试"
    return "请求处理失败，请稍后重试"


def resolve_enabled_capabilities(
    db: Session,
    user_id: str,
    request_skills: Optional[List[str]] = None,
    request_agents: Optional[List[str]] = None,
    request_mcps: Optional[List[str]] = None,
):
    """Resolve effective enabled capabilities, merging request overrides with DB state."""
    if request_skills is not None and request_agents is not None and request_mcps is not None:
        return request_skills, request_agents, request_mcps

    db_skills, db_agents, db_mcps = resolve_all_runtime_enabled(db, user_id)
    return (
        request_skills if request_skills is not None else db_skills,
        request_agents if request_agents is not None else db_agents,
        request_mcps if request_mcps is not None else db_mcps,
    )


def build_runtime_context(
    *,
    model_name: Optional[str],
    user_id: str,
    chat_id: str,
    enable_thinking: bool = False,
    uploaded_files: Optional[List[Dict[str, Any]]] = None,
    enabled_skills: Optional[List[str]] = None,
    enabled_agents: Optional[List[str]] = None,
    enabled_mcps: Optional[List[str]] = None,
    enabled_kbs: Optional[List[str]] = None,
    memory_enabled: bool = False,
    memory_write_enabled: bool = False,
    reranker_enabled: bool = False,
) -> Dict[str, Any]:
    """Build the runtime context dict consumed by workflow.py."""
    return {
        "model_name": model_name,
        "user_id": user_id,
        "chat_id": chat_id,
        "enable_thinking": enable_thinking,
        "uploaded_files": uploaded_files or [],
        "enabled_skills": enabled_skills,
        "enabled_agents": enabled_agents,
        "enabled_mcps": enabled_mcps,
        "enabled_kbs": enabled_kbs,
        "memory_enabled": memory_enabled,
        "memory_write_enabled": memory_write_enabled,
        "reranker_enabled": reranker_enabled,
    }


_MAX_HISTORY_SUMMARY_CHARS = 4000  # soft cap on total historical summary injection
_MAX_CHAT_MESSAGES_SCANNED = 500   # hard cap on message lookback when aggregating file refs


def _extract_message_file_ids(msg) -> List[str]:
    """Pull all file_ids referenced by a ChatMessage, regardless of role.

    User messages carry attachments in ``extra_data["attachments"]``;
    assistant messages carry AI-generated file refs in
    ``extra_data["artifacts"]``. Both are flat lists of ``{file_id, ...}``
    dicts. Inbound channel messages mirror their attachments into
    ``attachments`` too (see ``core/channels/inbound.py``), so the same
    cross-turn scan covers web and channel uploads uniformly.
    """
    extra = msg.extra_data or {}
    out: List[str] = []
    for key in ("attachments", "artifacts"):
        items = extra.get(key) or []
        for item in items:
            fid = (item.get("file_id") or "").strip() if isinstance(item, dict) else ""
            if fid:
                out.append(fid)
    return out


def collect_historical_attachments(
    chat_id: Optional[str],
    user_id: str,
    exclude_file_ids: set,
) -> List[Dict[str, Any]]:
    """Collect all prior file references in this chat, regardless of provenance.

    Approach:
      1. Scan every ``ChatMessage`` in the chat — user messages contribute
         ``extra_data["attachments"]``, assistant messages contribute
         ``extra_data["artifacts"]`` (AI-generated files from tools).
      2. Join the resulting file_ids into ``Artifact`` rows by primary key.
         We do NOT filter Artifact by ``chat_id``, because an artifact
         imported from "My Space" keeps the chat_id of its origin chat
         but is legitimately referenced by messages in this chat.
      3. Enforce ownership (Artifact.user_id must match the requester) so
         a user can only see metadata for their own files.

    Returns entries ordered oldest-first (by message timestamp). Each has:
        ``{file_id, name, mime_type, summary, source, deleted?}``

    Soft-caps the total summary text at ``_MAX_HISTORY_SUMMARY_CHARS``,
    preserving the most recent items when the cap is exceeded.
    """
    if not chat_id or not user_id:
        return []

    try:
        from core.db.engine import SessionLocal
        from core.db.models import Artifact as ArtifactModel, ChatMessage
        from core.content.artifact_reader import (
            SOURCE_AI_GENERATED,
            SOURCE_USER_UPLOAD,
            infer_source,
        )
    except Exception:
        return []

    excluded = set(exclude_file_ids or set())

    # Step 1: pull file_ids in order from the most recent messages.
    # Cap lookback to avoid unbounded scans on very long conversations;
    # the final summary cap (_MAX_HISTORY_SUMMARY_CHARS) trims further.
    file_ids_in_order: List[str] = []
    seen_fids: set = set()
    with SessionLocal() as db:
        recent_desc = db.query(ChatMessage).filter(
            ChatMessage.chat_id == chat_id,
        ).order_by(ChatMessage.created_at.desc()).limit(_MAX_CHAT_MESSAGES_SCANNED).all()
        msgs = list(reversed(recent_desc))

        for m in msgs:
            for fid in _extract_message_file_ids(m):
                if fid in seen_fids or fid in excluded:
                    continue
                seen_fids.add(fid)
                file_ids_in_order.append(fid)

        if not file_ids_in_order:
            return []

        rows = db.query(ArtifactModel).filter(
            ArtifactModel.artifact_id.in_(file_ids_in_order),
        ).all()
        row_by_id = {r.artifact_id: r for r in rows}

    items: List[Dict[str, Any]] = []
    for fid in file_ids_in_order:
        art = row_by_id.get(fid)
        if art is None:
            items.append({
                "file_id": fid,
                "name": "（文件元信息丢失）",
                "deleted": True,
                "source": SOURCE_USER_UPLOAD if fid.startswith("ua_") else SOURCE_AI_GENERATED,
            })
            continue
        if art.user_id != user_id or art.deleted_at is not None:
            items.append({
                "file_id": fid,
                "name": art.filename or fid,
                "deleted": True,
                "source": infer_source(art),
            })
            continue
        items.append({
            "file_id": fid,
            "name": art.filename or art.title or fid,
            "mime_type": art.mime_type,
            "summary": art.summary or "",
            "source": infer_source(art),
            "deleted": False,
        })

    # Soft cap: keep most recent items whose cumulative summary fits budget.
    result: List[Dict[str, Any]] = []
    total_chars = 0
    for it in reversed(items):
        piece = len(it.get("summary") or "") + len(it.get("name") or "") + 80
        if result and total_chars + piece > _MAX_HISTORY_SUMMARY_CHARS:
            break
        result.append(it)
        total_chars += piece
    result.reverse()
    return result
