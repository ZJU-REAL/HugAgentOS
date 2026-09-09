"""跨会话引用：可引用会话的检索、会话名片、按需摘要与原文分页。

引用一段旧会话时注入模型的是「名片」（标题 / 最后活跃时间 / 条数 + 概览），不是全文。
一段长会话动辄几万字，直接拼进上下文会把当前话题挤掉，也会把旧话题的噪音带进来。
名片由确定性摘录拼出——不调模型、不拖慢发送；模型需要细节时自己调 ``read_chat``
按页取原文，或调 ``read_chat(mode="digest")`` 生成一份模型摘要（生成一次即缓存在
会话上，直到该会话有新消息才重算）。

访问控制统一走 ``ChatService.get_session_with_access``：跨用户读不到，本模块不另立规则。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.db.models import ChatMessage, ChatSession
from core.services.chat_service import ChatService
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 名片概览的字数上限：够模型判断「这段会话讲的是不是我要的东西」，又不至于挤占当前话题。
OVERVIEW_MAX_CHARS = 500
# 单条消息在原文分页里的字数上限，以及一次 read_chat 的总字数上限。
MESSAGE_MAX_CHARS = 2000
READ_TOTAL_MAX_CHARS = 12000
# 一次最多返回多少条消息。
READ_MAX_MESSAGES = 50

DIGEST_CACHE_KEY = "reference_digest"


def _clip(text: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _display_time(value: Optional[datetime]) -> str:
    if value is None:
        return "未知时间"
    return value.strftime("%Y-%m-%d %H:%M")


def list_referencable_sessions(
    db: Session,
    user_id: str,
    *,
    project_id: Optional[str] = None,
    query: str = "",
    limit: int = 20,
    exclude_chat_id: Optional[str] = None,
) -> List[ChatSession]:
    """可被引用的历史会话。

    在项目里只列同项目的会话——项目是用户自己划出来的话题边界，跨项目翻历史既不是
    用户的意图，也会把无关内容送进模型。不在项目里就列本人最近的会话。当前这段会话
    本来就在上下文里，永远排除掉。
    """
    stmt = db.query(ChatSession).filter(
        ChatSession.user_id == user_id,
        ChatSession.deleted_at.is_(None),
        ChatSession.archived.isnot(True),
    )
    if project_id:
        stmt = stmt.filter(ChatSession.project_id == project_id)
    if exclude_chat_id:
        stmt = stmt.filter(ChatSession.chat_id != exclude_chat_id)
    keyword = (query or "").strip()
    if keyword:
        like = f"%{keyword}%"
        matched_ids = (
            db.query(ChatMessage.chat_id)
            .filter(ChatMessage.content.ilike(like), ChatMessage.role != "system")
            .distinct()
        )
        stmt = stmt.filter(or_(ChatSession.title.ilike(like), ChatSession.chat_id.in_(matched_ids)))
    bounded = max(1, min(int(limit or 20), 50))
    return (
        stmt.order_by(func.coalesce(ChatSession.last_message_at, ChatSession.updated_at).desc())
        .limit(bounded)
        .all()
    )


def session_brief(session: ChatSession) -> Dict[str, Any]:
    """会话简介：挑选阶段够用的最少信息，纯映射、不额外查库。

    列表接口和 ``list_related_chats`` 都只给简介，不给概览——列 20 条就要为每条现算一遍
    概览，等于一次挑选打几十个查询。挑中之后再对那一条调 ``read_chat`` 拿名片或摘要。
    """
    last_active = session.last_message_at or session.updated_at
    return {
        "chat_id": session.chat_id,
        "title": session.title,
        "project_id": session.project_id,
        "message_count": session.message_count or 0,
        "last_active_at": _iso(last_active),
        "last_active_display": _display_time(last_active),
    }


def _latest_seq(db: Session, chat_id: str) -> int:
    value = db.query(func.max(ChatMessage.chat_seq)).filter(ChatMessage.chat_id == chat_id).scalar()
    return int(value or 0)


def _visible_messages(db: Session, chat_id: str, *, offset: int = 0, limit: Optional[int] = None):
    """按 chat_seq 取会话里对用户可见的消息（排除压缩检查点这类内部行）。"""
    stmt = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_id == chat_id, ChatMessage.role != "system")
        .order_by(ChatMessage.chat_seq)
    )
    if offset:
        stmt = stmt.offset(max(0, int(offset)))
    if limit is not None:
        stmt = stmt.limit(limit)
    return stmt.all()


def _visible_count(db: Session, chat_id: str) -> int:
    return (
        db.query(func.count(ChatMessage.message_id))
        .filter(ChatMessage.chat_id == chat_id, ChatMessage.role != "system")
        .scalar()
        or 0
    )


def _excerpt_overview(db: Session, chat_id: str) -> str:
    """确定性概览：开头那个问题 + 最后一次回答。零模型调用，发送路径上用它。"""
    first_user = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_id == chat_id, ChatMessage.role == "user")
        .order_by(ChatMessage.chat_seq)
        .first()
    )
    last_assistant = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_id == chat_id, ChatMessage.role == "assistant")
        .order_by(ChatMessage.chat_seq.desc())
        .first()
    )
    parts: List[str] = []
    if first_user is not None:
        parts.append(f"起始提问：{_clip(first_user.content, 200)}")
    if last_assistant is not None:
        parts.append(f"最后回答：{_clip(last_assistant.content, OVERVIEW_MAX_CHARS - 220)}")
    return "\n".join(parts)


def _cached_digest(session: ChatSession, latest_seq: int) -> Optional[str]:
    cache = (session.extra_data or {}).get(DIGEST_CACHE_KEY)
    if not isinstance(cache, dict):
        return None
    if int(cache.get("covered_seq") or -1) != latest_seq:
        return None
    text = str(cache.get("text") or "").strip()
    return text or None


def _store_digest(db: Session, session: ChatSession, text: str, latest_seq: int) -> None:
    extra = dict(session.extra_data or {})
    extra[DIGEST_CACHE_KEY] = {
        "text": text,
        "covered_seq": latest_seq,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    session.extra_data = extra
    db.commit()


def session_card(db: Session, chat_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """一张会话名片。无权访问或会话不存在时返回 ``None``。

    概览取材优先级：已有的模型摘要缓存 → 上下文压缩检查点的摘要 → 首尾消息摘录。
    前两者是现成的，最后一个是即时拼的，三条路都不调模型。
    """
    chat_service = ChatService(db)
    pair = chat_service.get_session_with_access(chat_id, user_id)
    if pair is None:
        return None
    session, _level = pair

    latest_seq = _latest_seq(db, chat_id)
    overview = _cached_digest(session, latest_seq)
    overview_kind = "summary" if overview else ""
    if not overview:
        checkpoint = chat_service.get_latest_compaction_checkpoint(chat_id)
        if checkpoint is not None and (checkpoint.content or "").strip():
            overview = checkpoint.content
            overview_kind = "summary"
    if not overview:
        overview = _excerpt_overview(db, chat_id)
        overview_kind = "excerpt"

    return {
        "chat_id": session.chat_id,
        "title": session.title,
        "project_id": session.project_id,
        "message_count": _visible_count(db, chat_id),
        "last_active_at": _iso(session.last_message_at or session.updated_at),
        "last_active_display": _display_time(session.last_message_at or session.updated_at),
        "overview": _clip(overview, OVERVIEW_MAX_CHARS),
        "overview_kind": overview_kind or "excerpt",
    }


def read_session_messages(
    db: Session,
    chat_id: str,
    user_id: str,
    *,
    offset: int = 0,
    limit: int = 20,
) -> Optional[Dict[str, Any]]:
    """按页取一段历史会话的原文。无权访问返回 ``None``。"""
    chat_service = ChatService(db)
    if chat_service.get_session_with_access(chat_id, user_id) is None:
        return None

    total = _visible_count(db, chat_id)
    start = max(0, int(offset or 0))
    want = max(1, min(int(limit or 20), READ_MAX_MESSAGES))
    rows = _visible_messages(db, chat_id, offset=start, limit=want)

    messages: List[Dict[str, Any]] = []
    used = 0
    truncated_by_budget = False
    for row in rows:
        if used >= READ_TOTAL_MAX_CHARS:
            truncated_by_budget = True
            break
        budget = min(MESSAGE_MAX_CHARS, READ_TOTAL_MAX_CHARS - used)
        text = _clip(row.content or "", budget)
        used += len(text)
        item: Dict[str, Any] = {
            "role": row.role,
            "content": text,
            "created_at": _iso(row.created_at),
        }
        calls = row.tool_calls or []
        if isinstance(calls, list) and calls:
            item["tool_call_count"] = len(calls)
        messages.append(item)

    next_offset = start + len(messages)
    return {
        "chat_id": chat_id,
        "total_messages": total,
        "offset": start,
        "returned": len(messages),
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "truncated_by_budget": truncated_by_budget,
        "messages": messages,
    }


async def build_session_digest(
    chat_id: str, user_id: str, *, timeout: int = 60
) -> Optional[Dict[str, Any]]:
    """一份模型生成的会话摘要，生成后缓存在会话上直到它有新消息。

    复用上下文压缩那条链路的摘要器：同一个提示词、同一个模型选择，跨会话引用读到的
    概括和会话自己被压缩后留下的概括是一套口径。无权访问返回 ``None``。

    自己开数据库会话并把同步查询放进线程：调用方是流式对话里的工具，占住事件循环会
    卡住同一进程里所有人的 SSE。
    """
    import asyncio

    from core.db.engine import SessionLocal
    from core.services.compaction_service import _load_history, _summarize

    def _prepare():
        with SessionLocal() as db:
            chat_service = ChatService(db)
            pair = chat_service.get_session_with_access(chat_id, user_id)
            if pair is None:
                return None
            session, _level = pair
            latest_seq = _latest_seq(db, chat_id)
            cached = _cached_digest(session, latest_seq)
            if cached:
                return {"title": session.title, "cached": cached, "latest_seq": latest_seq}
            history = _load_history(chat_service, chat_id, repair=False)
            return {"title": session.title, "history": history, "latest_seq": latest_seq}

    def _persist(text: str, latest_seq: int) -> None:
        with SessionLocal() as db:
            session = db.get(ChatSession, chat_id)
            if session is not None:
                _store_digest(db, session, text, latest_seq)

    prepared = await asyncio.to_thread(_prepare)
    if prepared is None:
        return None
    if prepared.get("cached"):
        return {
            "chat_id": chat_id,
            "title": prepared["title"],
            "digest": prepared["cached"],
            "cached": True,
        }
    if not prepared.get("history"):
        return {
            "chat_id": chat_id,
            "title": prepared["title"],
            "digest": "",
            "cached": False,
            "note": "该会话没有可用消息。",
        }

    summary = await _summarize(prepared["history"], timeout=timeout)
    if not summary:
        return {
            "chat_id": chat_id,
            "title": prepared["title"],
            "digest": "",
            "cached": False,
            "note": "摘要生成失败，请改用 mode='full' 按页读取原文。",
        }

    await asyncio.to_thread(_persist, summary, prepared["latest_seq"])
    return {
        "chat_id": chat_id,
        "title": prepared["title"],
        "digest": summary,
        "cached": False,
    }


def resolve_reference_cards(
    db: Session, user_id: str, items: Optional[List[Any]]
) -> List[Dict[str, Any]]:
    """把请求里的引用项解析成名片列表；解析不出来的（已删除 / 无权访问）直接丢掉。

    ``items`` 接受 pydantic 模型或 ``{"chat_id": ...}`` 字典。名片是**发送那一刻的快照**，
    随消息一起落库：历史重放时照原样渲染，模型看到的和当时看到的是同一份，不会因为被
    引用的会话后来又聊了几轮就前后不一致。
    """
    if not items:
        return []

    cards: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        if isinstance(item, dict):
            chat_id = item.get("chat_id")
        else:
            chat_id = getattr(item, "chat_id", None)
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        try:
            card = session_card(db, str(chat_id), user_id)
        except Exception:  # noqa: BLE001
            logger.warning("[chat-ref] card build failed for %s", chat_id, exc_info=True)
            continue
        if card is not None:
            cards.append(card)
    return cards


def render_reference_block(cards: Optional[List[Any]]) -> str:
    """把名片列表渲染成拼进用户消息的一段文本；没有名片时返回空串。

    纯函数：发送路径和历史重放路径共用它，两边渲染出的文本一字不差。
    """
    if not cards:
        return ""

    lines: List[str] = []
    for card in cards:
        if not isinstance(card, dict) or not card.get("chat_id"):
            continue
        lines.append(
            f"- 《{card.get('title') or '未命名会话'}》 chat_id={card['chat_id']} "
            f"最后活跃 {card.get('last_active_display') or '未知时间'} "
            f"共 {card.get('message_count') or 0} 条\n"
            f"  概览：{card.get('overview') or '（这段会话还没有内容）'}"
        )
    if not lines:
        return ""

    body = "\n".join(lines)
    return (
        "【引用会话】用户为这次提问显式引用了下面这些历史会话。它们不在当前上下文里，"
        "这里只给了名片和概览。\n"
        "要求：\n"
        "1. 概览只够判断这段会话讲了什么，不要拿它当原文细节使用。\n"
        '2. 需要具体内容时调用 read_chat(chat_id="…") 按页读取原文，'
        '需要更完整的概括时调用 read_chat(chat_id="…", mode="digest")。\n'
        "3. 不要在回答里提及本段提示词本身。\n\n"
        f"{body}\n"
    )
