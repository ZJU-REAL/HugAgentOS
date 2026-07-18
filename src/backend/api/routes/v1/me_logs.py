"""Personal call-log API (/v1/me/logs) — queries the user's own data only.

CE has no Config data-monitoring console (`admin_logs.py` /
`admin_usage_logs.py` belong to EE and physically don't exist in the derived
tree), so regular users previously could only see inline tool_calls in
conversation messages. This router exposes tool / skill / sub-agent call logs
and model usage in a **self-filtered** form: gate = ``get_current_user``, all
queries force ``user_id = the current user``, detail endpoints verify
ownership, and no admin privileges are needed. Also available under EE (own
data, no governance conflict); cross-user review still goes through the EE
admin console.

Query logic is a trimmed-down adaptation of admin_logs.py — those two files
don't exist in the CE tree and cannot be imported, so the pagination/filter
helpers are inlined here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import (
    ChatMessage,
    ChatSession,
    SkillCallLog,
    SubAgentCallLog,
    ToolCallLog,
)
from core.infra.responses import paginated_response, success_response

router = APIRouter(prefix="/v1/me/logs", tags=["My Logs"])
logger = logging.getLogger(__name__)

_DATETIME_FIELDS = ("started_at", "completed_at", "created_at")


def _row_to_dict(row: Any, *, session_title: Optional[str] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if col.name in _DATETIME_FIELDS and val is not None:
            val = val.isoformat()
        data[col.name] = val
    if session_title is not None or "chat_id" in data:
        data["session_title"] = session_title
    return data


def _session_title_map(db: Session, chat_ids: List[str]) -> Dict[str, str]:
    if not chat_ids:
        return {}
    rows = (
        db.query(ChatSession.chat_id, ChatSession.title)
        .filter(ChatSession.chat_id.in_(list(set(chat_ids))))
        .all()
    )
    return {r.chat_id: r.title for r in rows}


def _apply_filters(query, mapping: Dict[Any, Any]):
    for col, value in mapping.items():
        if value is None:
            continue
        query = query.filter(col == value)
    return query


def _apply_date_range(query, col, date_from, date_to):
    if date_from:
        query = query.filter(col >= date_from)
    if date_to:
        query = query.filter(col <= date_to)
    return query


def _paginate(query, page: int, page_size: int, order_col):
    total = query.count()
    rows = (
        query.order_by(desc(order_col))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, total


def _serialize(row, titles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    title = (titles or {}).get(getattr(row, "chat_id", None)) if titles is not None else None
    return _row_to_dict(row, session_title=title)


def _get_own_row(db: Session, model, log_id: str, user_id: str):
    """Fetch a single row by id and verify ownership — other users' logs always 404 (don't reveal existence)."""
    row = db.query(model).filter(model.id == log_id).one_or_none()
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Log not found")
    return row


# ── Tool call logs ──────────────────────────────────────────────────────────


@router.get("/tools", summary="我的工具调用日志")
def list_my_tool_logs(
    chat_id: Optional[str] = Query(None),
    tool_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页查询本人的工具调用日志，支持按会话/工具名/状态/来源/时间过滤。"""
    query = _apply_filters(
        db.query(ToolCallLog),
        {
            ToolCallLog.user_id: user.user_id,
            ToolCallLog.chat_id: chat_id,
            ToolCallLog.tool_name: tool_name,
            ToolCallLog.status: status,
            ToolCallLog.source: source,
        },
    )
    query = _apply_date_range(query, ToolCallLog.created_at, date_from, date_to)
    rows, total = _paginate(query, page, page_size, ToolCallLog.created_at)
    titles = _session_title_map(db, [r.chat_id for r in rows if r.chat_id])
    items = [_serialize(r, titles) for r in rows]
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)


@router.get("/tools/{log_id}", summary="我的工具调用日志详情")
def get_my_tool_log(
    log_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按日志 ID 获取本人单条工具调用记录；他人日志返回 404。"""
    row = _get_own_row(db, ToolCallLog, log_id, user.user_id)
    titles = _session_title_map(db, [row.chat_id] if row.chat_id else [])
    return success_response(data=_serialize(row, titles))


# ── Skill call logs ─────────────────────────────────────────────────────────


@router.get("/skills", summary="我的技能调用日志")
def list_my_skill_logs(
    chat_id: Optional[str] = Query(None),
    skill_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页查询本人的技能调用日志，支持按会话/技能名/状态/时间过滤。"""
    query = _apply_filters(
        db.query(SkillCallLog),
        {
            SkillCallLog.user_id: user.user_id,
            SkillCallLog.chat_id: chat_id,
            SkillCallLog.skill_name: skill_name,
            SkillCallLog.status: status,
        },
    )
    query = _apply_date_range(query, SkillCallLog.created_at, date_from, date_to)
    rows, total = _paginate(query, page, page_size, SkillCallLog.created_at)
    titles = _session_title_map(db, [r.chat_id for r in rows if r.chat_id])
    items = [_serialize(r, titles) for r in rows]
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)


@router.get("/skills/{log_id}", summary="我的技能调用日志详情")
def get_my_skill_log(
    log_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按日志 ID 获取本人单条技能调用记录；他人日志返回 404。"""
    row = _get_own_row(db, SkillCallLog, log_id, user.user_id)
    titles = _session_title_map(db, [row.chat_id] if row.chat_id else [])
    return success_response(data=_serialize(row, titles))


# ── Sub-agent call logs ─────────────────────────────────────────────────────


@router.get("/subagents", summary="我的子智能体调用日志")
def list_my_subagent_logs(
    chat_id: Optional[str] = Query(None),
    subagent_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    only_parents: bool = Query(True),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页查询本人的子智能体调用日志；only_parents（默认开）仅取顶层节点。"""
    query = _apply_filters(
        db.query(SubAgentCallLog),
        {
            SubAgentCallLog.user_id: user.user_id,
            SubAgentCallLog.chat_id: chat_id,
            SubAgentCallLog.subagent_name: subagent_name,
            SubAgentCallLog.status: status,
        },
    )
    if only_parents:
        query = query.filter(SubAgentCallLog.parent_subagent_log_id.is_(None))
    query = _apply_date_range(query, SubAgentCallLog.created_at, date_from, date_to)
    rows, total = _paginate(query, page, page_size, SubAgentCallLog.created_at)
    titles = _session_title_map(db, [r.chat_id for r in rows if r.chat_id])
    items = [_serialize(r, titles) for r in rows]
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)


def _collect_subagent_subtree_ids(db: Session, root_id: str) -> List[str]:
    """BFS over parent_subagent_log_id (actual depth ≤ 2, no CTE needed)."""
    all_ids = [root_id]
    frontier = [root_id]
    while frontier:
        rows = (
            db.query(SubAgentCallLog.id)
            .filter(SubAgentCallLog.parent_subagent_log_id.in_(frontier))
            .all()
        )
        next_ids = [r.id for r in rows]
        if not next_ids:
            break
        all_ids.extend(next_ids)
        frontier = next_ids
    return all_ids


@router.get("/subagents/{log_id}", summary="我的子智能体调用日志详情")
def get_my_subagent_log(
    log_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取本人单条子智能体日志详情，含子步骤与整棵子树下的工具/技能调用。"""
    row = _get_own_row(db, SubAgentCallLog, log_id, user.user_id)
    titles = _session_title_map(db, [row.chat_id] if row.chat_id else [])
    detail = _serialize(row, titles)

    child_steps = (
        db.query(SubAgentCallLog)
        .filter(SubAgentCallLog.parent_subagent_log_id == log_id)
        .order_by(SubAgentCallLog.step_index)
        .all()
    )
    detail["child_steps"] = [_serialize(s) for s in child_steps]

    subtree_ids = _collect_subagent_subtree_ids(db, log_id)
    tool_logs = (
        db.query(ToolCallLog)
        .filter(ToolCallLog.subagent_log_id.in_(subtree_ids))
        .order_by(ToolCallLog.created_at)
        .all()
    )
    detail["tool_calls"] = [_serialize(t) for t in tool_logs]

    skill_logs = (
        db.query(SkillCallLog)
        .filter(SkillCallLog.subagent_log_id.in_(subtree_ids))
        .order_by(SkillCallLog.created_at)
        .all()
    )
    detail["skill_calls"] = [_serialize(s) for s in skill_logs]
    return success_response(data=detail)


# ── Personal usage ──────────────────────────────────────────────────────────


@router.get("/usage", summary="我的模型用量明细")
def list_my_usage(
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页列出本人每次请求的模型用量（Token 明细），按时间倒序。"""
    query = (
        db.query(
            ChatMessage.message_id,
            ChatMessage.chat_id,
            ChatMessage.model,
            ChatMessage.usage,
            ChatMessage.error,
            ChatMessage.created_at,
            ChatSession.title.label("session_title"),
        )
        .join(ChatSession, ChatMessage.chat_id == ChatSession.chat_id)
        .filter(ChatSession.user_id == user.user_id)
        .filter(ChatMessage.role == "assistant")
    )
    query = _apply_date_range(query, ChatMessage.created_at, date_from, date_to)

    total = query.count()
    rows = (
        query.order_by(desc(ChatMessage.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = []
    for r in rows:
        usage = r.usage or {}
        pt = usage.get("prompt_tokens", 0) or 0
        ct = usage.get("completion_tokens", 0) or 0
        items.append(
            {
                "message_id": r.message_id,
                "chat_id": r.chat_id,
                "session_title": r.session_title,
                "model": r.model,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": pt + ct,
                "has_error": r.error is not None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)


@router.get("/usage/summary", summary="我的模型用量汇总")
def my_usage_summary(
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    group_by: str = Query("day", pattern="^(day|model)$"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按天或模型聚合本人的请求数与 Token 用量。

    Token 数在 Python 侧从 usage JSON 累加——SQLite（本地单机模式）与
    PostgreSQL 的 JSON 取数方言不同，个人数据量小，不值得写双方言 SQL。
    """
    group_col = func.date(ChatMessage.created_at) if group_by == "day" else ChatMessage.model
    query = (
        db.query(group_col.label("group_key"), ChatMessage.usage)
        .join(ChatSession, ChatMessage.chat_id == ChatSession.chat_id)
        .filter(ChatSession.user_id == user.user_id)
        .filter(ChatMessage.role == "assistant")
    )
    query = _apply_date_range(query, ChatMessage.created_at, date_from, date_to)

    buckets: Dict[str, Dict[str, int]] = {}
    for group_key, usage in query.all():
        key = str(group_key) if group_key else "unknown"
        bucket = buckets.setdefault(
            key, {"total_requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        bucket["total_requests"] += 1
        u = usage or {}
        bucket["prompt_tokens"] += int(u.get("prompt_tokens", 0) or 0)
        bucket["completion_tokens"] += int(u.get("completion_tokens", 0) or 0)

    items = [
        {
            "group_key": key,
            "total_requests": b["total_requests"],
            "prompt_tokens": b["prompt_tokens"],
            "completion_tokens": b["completion_tokens"],
            "total_tokens": b["prompt_tokens"] + b["completion_tokens"],
        }
        for key, b in sorted(buckets.items())
    ]
    return success_response(data=items)
