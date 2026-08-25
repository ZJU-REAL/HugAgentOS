"""Chat session management + streaming chat API routes (v1)."""

import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from api.schemas import AttachmentItem, ChatRequest, ChatResponse
from api.routes.v1.chat_admission import chat_busy_http_exception
from core.auth.backend import UserContext, get_current_user
from core.auth.permissions_iface import can_delete_session
from core.chat.context import build_effective_user_message as _build_effective_user_message
from core.chat.context import (
    build_runtime_context,
    generate_smart_title,
    now_iso,
    resolve_db_user_id,
    resolve_enabled_capabilities,
    resolve_user_facing_error,
)
from core.db.engine import SessionLocal, get_db
from core.db.models import MessageFeedback
from core.infra.exceptions import ResourceNotFoundError, ServiceUnavailableError
from core.infra.logging import get_logger
from core.infra.responses import (
    created_response,
    paginated_response,
    sse_response,
    success_response,
)
from core.llm.message_compat import strip_thinking
from core.llm.tools.user_questions import MAX_QUESTIONS as USER_QUESTION_MAX
from core.services import ChatService, UserService
from core.services.compaction_service import get_compaction_context_state
from core.services.model_config import ModelConfigService
from core.services.project_scope import project_scope_from_context
from core.services.user_model_selection import (
    UserModelSelectionError,
    resolve_effective_chat_model_name,
    resolve_user_model_provider_id,
)
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from orchestration.followups import get_followup_generator
from orchestration.workflow import astream_chat_workflow
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["Sessions"])


# ── Shared helpers for streaming SSE event processing ────────────────────
# Moved to neutral lower layers so routing/* and core/* can use them without
# importing this API route module. Re-exported under their original names so
# the route handlers below stay unchanged.
from core.chat.tool_log import (  # noqa: E402
    build_thinking_event,
    build_tool_call_delta_event,
    build_tool_call_event,
    build_tool_call_start_event,
    build_tool_result_event,
    build_user_question_event,
    build_user_question_resolved_event,
)
from core.services.artifact_service import persist_artifacts as _persist_artifacts  # noqa: E402


def _ensure_main_model_configured() -> None:
    """Fail fast with a user-facing error when the main chat model is missing."""
    resolved = ModelConfigService.get_instance().resolve("main_agent")
    if resolved is not None:
        return
    raise HTTPException(
        status_code=503,
        detail="当前未配置主对话模型，请先在管理后台配置模型供应商并绑定 main_agent 角色。",
    )


# Request/Response Models
class CreateChatRequest(BaseModel):
    """Request model for creating a chat session."""

    title: Optional[str] = Field("新对话", description="Chat session title")
    metadata: Optional[dict] = Field(default_factory=dict, description="Additional metadata")


class UpdateChatRequest(BaseModel):
    """Request model for updating a chat session."""

    title: Optional[str] = Field(None, description="Chat session title")
    pinned: Optional[bool] = Field(None, description="Pin status")
    favorite: Optional[bool] = Field(None, description="Favorite status")
    metadata: Optional[dict] = Field(None, description="Additional metadata")


class UpdateSidebarOrderRequest(BaseModel):
    """Request model for persisting the sidebar's manual (drag-and-drop) order."""

    order: List[str] = Field(default_factory=list, description="Chat ids in manual order")


class UserQuestionAnswerItem(BaseModel):
    """One browser answer correlated to the model's stable question id."""

    id: str = Field(..., min_length=1, max_length=64)
    # No numeric cap: which option ids are legal for this question — and how
    # many a multi-select may carry — is decided against the pending question
    # itself in ``user_questions.normalize_answers``.
    selected: List[str] = Field(default_factory=list)
    custom: Optional[str] = Field(None, max_length=2000)
    skipped: bool = False


class UserQuestionAnswerBody(BaseModel):
    # The ceiling is the domain's own question cap, imported rather than
    # restated: an answer set must match the pending questions one-for-one, so
    # a locally-invented bound here silently rejects otherwise valid rounds.
    answers: List[UserQuestionAnswerItem] = Field(..., min_length=1, max_length=USER_QUESTION_MAX)


# Sidebar manual order lives in users_shadow.metadata (no schema migration needed):
# it is a pure UI preference of "which chat sits where", not session state.
SIDEBAR_ORDER_KEY = "sidebar_chat_order"
SIDEBAR_ORDER_MAX = 500


def _session_to_dict(s) -> dict:
    """Convert a ChatSession ORM object to the edition-neutral API response."""
    return {
        "chat_id": s.chat_id,
        "title": s.title,
        "user_id": s.user_id,
        "message_count": s.message_count,
        "pinned": s.pinned,
        "favorite": s.favorite,
        # Project attachment (if any) — the frontend uses this to bind the chat back to the project and auto-attaches project_id when sending new messages
        "project_id": s.project_id,
        "metadata": s.extra_data or {},
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _session_view_for_user(db: Session, s, user_id: str, level: str) -> dict:
    """Render a session for the current user, then let the edition extend it."""
    from core.services.chat_edition import extend_session_view

    return extend_session_view(db, s, user_id, level, _session_to_dict(s))


def _message_to_dict(m) -> dict:
    """Convert a ChatMessage ORM object to API response dict."""
    return {
        "message_id": m.message_id,
        "chat_id": m.chat_id,
        "chat_seq": m.chat_seq,
        "role": m.role,
        "content": m.content,
        "model": m.model,
        "tool_calls": m.tool_calls,
        "metadata": m.extra_data or {},
        "created_at": m.created_at.isoformat(),
    }


# 作业唤醒消息的开头（``orchestration/job_wakeup.py`` 生成）。新写入的消息带
# ``extra_data.hidden_in_chat``；这两个前缀是给**标记上线之前**已经落库的老消息兜底的——
# 用户的会话里已经躺着一串「[系统] 进度播报：……」，只靠标记它们永远漏出来。
_WAKE_MESSAGE_PREFIXES = (
    "[系统] 进度播报：",
    "[系统] 你先前提交的批量作业",
)


def _is_internal_message(m) -> bool:
    """这条消息是写给模型的内部指令，不该出现在用户看到的聊天记录里。

    后台作业的唤醒轮（进度播报 / 终态交付）是以 user 角色落库的系统指令——模型必须看见它
    （历史另走 ``load_session_history``，不经过本接口），但用户看到的应该只是助手那句
    转述，而不是「请只用一两句话把上面的进度转述给用户」这种提示词本身。
    """
    extra = getattr(m, "extra_data", None) or {}
    if isinstance(extra, dict) and extra.get("hidden_in_chat"):
        return True
    content = getattr(m, "content", None)
    return (
        getattr(m, "role", "") == "user"
        and isinstance(content, str)
        and content.startswith(_WAKE_MESSAGE_PREFIXES)
    )


def _clean_id_list(raw: Optional[list]) -> List[str]:
    """Normalize a list of capability IDs: strip whitespace, remove empties."""
    if not isinstance(raw, list):
        return []
    return [str(s).strip() for s in raw if str(s).strip()]


@router.get("", summary="获取会话列表")
async def list_chats(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    sort: str = Query("-updated_at", description="Sort field"),
    filter: Optional[str] = Query(None, description="Filter conditions"),
    exclude_automation: bool = Query(False, description="Exclude automation-generated chats"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get paginated list of chat sessions for the current user.

    Supports filtering by:
    - pinned=true - Only pinned sessions
    - favorite=true - Only favorite sessions
    - exclude_automation=true - Hide automation-generated sessions

    Supports sorting by:
    - -updated_at (default) - Most recently updated first
    - updated_at - Oldest updated first
    - -created_at - Most recently created first
    - created_at - Oldest created first
    """
    chat_service = ChatService(db)

    # Parse filters
    pinned_only = filter == "pinned=true" if filter else False
    favorite_only = filter == "favorite=true" if filter else False

    # Get sessions
    sessions, total, total_pages = chat_service.list_sessions(
        user_id=user.user_id,
        page=page,
        page_size=page_size,
        pinned_only=pinned_only,
        favorite_only=favorite_only,
        exclude_automation=exclude_automation,
    )

    items = [_session_to_dict(s) for s in sessions]

    return paginated_response(
        items=items,
        page=page,
        page_size=page_size,
        total_items=total,
        message="Chat sessions retrieved successfully",
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建新会话")
async def create_chat(
    request: CreateChatRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a new chat session.

    The session is automatically associated with the current authenticated user.
    """
    chat_service = ChatService(db)

    session = chat_service.create_session(
        user_id=user.user_id, title=request.title, extra_data=request.metadata
    )

    return created_response(
        data=_session_to_dict(session), message="Chat session created successfully"
    )


@router.get("/search", summary="搜索会话")
async def search_chats(
    q: str = Query(..., description="Search keyword"),
    scope: str = Query(
        "title", description="Search scope: 'title' or 'all' (title + message content)"
    ),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Search chat sessions by title and optionally message content.

    - scope=title (default): search title only
    - scope=all: search both title and message content

    Returns sessions with match_type ("title" or "content") and matched_snippet for content matches.
    """
    chat_service = ChatService(db)

    results, total = chat_service.search_sessions(
        user_id=user.user_id,
        query=q,
        page=page,
        page_size=page_size,
        scope=scope,
    )

    items = []
    for r in results:
        item = _session_to_dict(r["session"])
        item["match_type"] = r["match_type"]
        item["matched_snippet"] = r["matched_snippet"]
        items.append(item)

    return success_response(
        data={"items": items, "total": total}, message="Search completed successfully"
    )


@router.get("/pending-confirms", summary="批量查询本人会话的待确认我的空间写操作")
async def list_pending_confirms(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """前端刷新/首屏加载时一次性拉取——用于在侧边栏对应会话上点亮蓝点。

    注册表是进程内 per-chat（§13），逐个对候选 chat_id 做归属校验，只下发
    本人会话；待确认项数量天然很小（确认队列），逐条 DB 校验开销可忽略。

    注意：本路由必须声明在 ``GET /{chat_id}`` **之前**，否则会被 path
    参数路由吞掉（FastAPI 按声明顺序匹配）。
    """
    from core.llm.tools import _myspace_confirm as _mc

    chat_service = ChatService(db)
    items = []
    for cid in _mc.list_pending_chat_ids():
        if chat_service.get_session(cid, user.user_id) is None:
            continue
        # Cannot just take "the latest one" (get_pending): when the latest is a
        # design_pick it would mask an earlier write-confirm in the same chat and
        # the blue dot would never light up. Prefer the latest write-confirm
        # pending; fall back to design_pick only if there is none (the frontend
        # renders that as merely lighting the blue dot).
        pendings = _mc.get_all_pending(cid)
        if not pendings:
            continue
        confirms = [p for p in pendings if p.get("kind") != _mc.KIND_DESIGN_PICK]
        rec = confirms[-1] if confirms else pendings[-1]
        items.append({"chat_id": cid, **rec})
    return success_response(data={"items": items})


@router.get("/pending-user-questions", summary="批量查询本人会话的待回答问题")
async def list_pending_user_questions(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Restore sidebar waiting indicators without opening every conversation."""

    from core.llm.tools import user_questions

    chat_service = ChatService(db)
    items = []
    for chat_id in user_questions.list_pending_chat_ids():
        if chat_service.get_session(chat_id, user.user_id) is None:
            continue
        items.extend(
            {"chat_id": chat_id, **request} for request in user_questions.get_all_pending(chat_id)
        )
    return success_response(data={"items": items})


def _dedup_id_list(raw: Optional[list]) -> List[str]:
    """Clean + de-duplicate an id list, preserving first-seen order."""
    seen: set = set()
    out: List[str] = []
    for cid in _clean_id_list(raw):
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


@router.get("/sidebar-order", summary="获取侧边栏手动排序")
async def get_sidebar_order(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """侧边栏对话列表的手动拖拽顺序（chat_id 序列）。

    没拖过的账号返回空数组——前端据此退回「置顶 + 最近更新」默认排序。

    注意：本路由必须声明在 ``GET /{chat_id}`` **之前**，否则会被 path 参数
    路由吞掉（FastAPI 按声明顺序匹配）。
    """
    user_settings = UserService(db).get_user_settings(str(user.user_id))
    order = _dedup_id_list(user_settings.get(SIDEBAR_ORDER_KEY))[:SIDEBAR_ORDER_MAX]
    return success_response(data={"order": order})


@router.put("/sidebar-order", summary="保存侧边栏手动排序")
async def update_sidebar_order(
    request: UpdateSidebarOrderRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """整表覆盖写入手动顺序；空数组 = 恢复默认排序。

    不校验 chat_id 是否存在：顺序表是纯 UI 偏好，已删除的会话留在表里也只是
    查不到对应项而被忽略，反倒省掉一次全表校验。超出上限的尾部直接截断。
    """
    order = _dedup_id_list(request.order)[:SIDEBAR_ORDER_MAX]
    UserService(db).update_user_metadata(
        user_id=str(user.user_id), patch={SIDEBAR_ORDER_KEY: order}
    )
    return success_response(data={"order": order})


@router.get("/{chat_id}", summary="获取会话详情")
async def get_chat(
    chat_id: str, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)
):
    """获取当前用户有权读取的会话详情。"""
    chat_service = ChatService(db)

    pair = chat_service.get_session_with_access(chat_id, str(user.user_id))
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    session, level = pair

    return success_response(
        data=_session_view_for_user(db, session, str(user.user_id), level),
        message="Chat session retrieved successfully",
    )


@router.patch("/{chat_id}", summary="更新会话")
async def update_chat(
    chat_id: str,
    request: UpdateChatRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新当前用户有权修改的会话元信息。"""
    chat_service = ChatService(db)
    user_id = str(user.user_id)

    pair = chat_service.get_session_with_access(chat_id, user_id)
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    session, level = pair

    # title: modifiable by admin / edit
    title_change: Optional[str] = None
    if request.title is not None:
        if level not in ("admin", "edit"):
            raise HTTPException(status_code=403, detail="只读共享会话，标题仅创建者可改")
        title_change = request.title

    # metadata: owner (admin) only
    metadata_change: Optional[dict] = None
    if request.metadata is not None:
        if level != "admin":
            raise HTTPException(status_code=403, detail="会话元数据仅创建者可改")
        metadata_change = request.metadata

    from core.services.chat_edition import update_member_state

    member_state_updated = False
    if request.pinned is not None or request.favorite is not None:
        member_state_updated = update_member_state(
            db,
            session,
            user_id,
            pinned=request.pinned,
            favorite=request.favorite,
        )
    if member_state_updated:
        pin_change = None
        fav_change = None
    else:
        # Non-shared: keep the old semantics, but only the owner may write
        if (request.pinned is not None or request.favorite is not None) and level != "admin":
            raise HTTPException(status_code=403, detail="非共享会话的置顶/收藏仅创建者可改")
        pin_change = request.pinned
        fav_change = request.favorite

    fields: Dict[str, Any] = {}
    if title_change is not None:
        fields["title"] = title_change
    if pin_change is not None:
        fields["pinned"] = pin_change
    if fav_change is not None:
        fields["favorite"] = fav_change
    if metadata_change is not None:
        fields["extra_data"] = metadata_change

    if fields:
        chat_service.update_session_fields(chat_id, fields, actor_user_id=user_id)

    # Reload and render from the current user's perspective
    pair2 = chat_service.get_session_with_access(chat_id, user_id)
    if pair2 is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    s2, level2 = pair2
    return success_response(
        data=_session_view_for_user(db, s2, user_id, level2),
        message="Chat session updated successfully",
    )


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除会话")
async def delete_chat(
    chat_id: str, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)
):
    """软删当前用户有权管理的会话。"""
    chat_service = ChatService(db)
    user_id = str(user.user_id)

    # First check whether the session is visible to the current user — return 404 if not, to avoid leaking its existence
    pair = chat_service.get_session_with_access(chat_id, user_id)
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    session, _level = pair

    if not can_delete_session(db, user_id, session):
        raise HTTPException(status_code=403, detail="共享会话仅创建者或项目管理员可删")

    chat_service.delete_session_force(chat_id, actor_user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{chat_id}/messages", summary="获取会话消息列表")
async def list_messages(
    chat_id: str,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取当前用户有权读取的会话消息列表；无权访问时返回 404。"""
    chat_service = ChatService(db)
    user_id = str(user.user_id)

    pair = chat_service.get_session_with_access(chat_id, user_id)
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)

    messages, total = chat_service.message_repo.list_by_chat(chat_id, page, page_size)
    items = [_message_to_dict(m) for m in messages if not _is_internal_message(m)]
    response = paginated_response(
        items=items,
        page=page,
        page_size=page_size,
        total_items=total,
        message="Messages retrieved successfully",
    )
    # Keep the complete transcript visible, while exposing the active
    # checkpoint baseline separately so context-usage estimates do not keep
    # counting messages that the model now sees only through the summary.
    response["data"]["context_compaction"] = get_compaction_context_state(chat_service, chat_id)
    return response


@router.get("/{chat_id}/messages/{message_id}/followups", summary="获取追问问题")
async def get_followups(
    chat_id: str,
    message_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return follow-up questions stored in a message's extra_data."""
    chat_service = ChatService(db)

    session = chat_service.get_session(chat_id, user.user_id)
    if not session:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)

    msg = chat_service.message_repo.get_by_id(message_id)
    if not msg or msg.chat_id != chat_id:
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)

    questions = (msg.extra_data or {}).get("follow_up_questions", [])
    return success_response(data={"follow_up_questions": questions})


# ── Streaming / Non-streaming chat ────────────────────────────────────────


def _authenticated_user_id(user: Optional[UserContext]) -> Optional[str]:
    if isinstance(user, UserContext):
        return user.user_id
    return None


def _resolve_selected_model_provider_id(
    db: Session, request: ChatRequest, user_id: str
) -> Optional[str]:
    try:
        return resolve_user_model_provider_id(db, request.model_provider_id, user_id=user_id)
    except UserModelSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_actual_chat_model_name(
    request: ChatRequest,
    selected_model_provider_id: Optional[str],
) -> Optional[str]:
    return resolve_effective_chat_model_name(
        selected_model_provider_id,
        fallback_model_name=request.model_name,
    )


def _resolve_chat_agent_targets(
    db: Session,
    request: ChatRequest,
    user_id: str,
) -> tuple[ChatRequest, Optional[str], str, Optional[Any]]:
    """Resolve persistent and per-turn direct sub-agent targets.

    ``agent_id`` binds a dedicated sub-agent conversation and is therefore
    persisted on the chat session. ``mention_agent_id`` is an explicit @mention
    delegation for this turn only. Older clients send only ``mention_name``;
    accept that form when it resolves to exactly one accessible enabled agent.

    A strict natural-language command (``调用「完整名称」子智能体：...``) is
    returned separately as ``explicit_command``. It must not be rewritten to
    ``mention_agent_id``. Both forms remain on the main-model stream and
    constrain its next real tool call to ``call_subagent``. Only a persistent
    ``agent_id`` conversation executes the selected sub-agent directly.
    """
    from core.llm.builtin_subagents import get_builtin_subagent, merge_builtin_subagents
    from core.services.user_agent_service import UserAgentService
    from core.services.user_service import UserService

    service = UserAgentService(db)
    disabled_ids = UserService(db).get_disabled_builtin_subagent_ids(user_id)
    available_delegates = merge_builtin_subagents(
        service.list_for_user(user_id),
        disabled_agent_ids=disabled_ids,
    )
    persistent_agent_name: Optional[str] = None
    execution_message = request.message
    explicit_command = None

    if request.agent_id:
        persistent = next(
            (item for item in available_delegates if item.get("agent_id") == request.agent_id),
            None,
        )
        if persistent is None:
            raise HTTPException(status_code=403, detail="无法访问该子智能体")
        persistent_agent_name = str(persistent["name"])

    mention_agent_id = request.mention_agent_id
    mention_agent_name = request.mention_name
    if not mention_agent_id and not mention_agent_name:
        from core.services.subagent_routing_service import parse_explicit_subagent_command

        explicit_command = parse_explicit_subagent_command(
            request.message,
            available_delegates,
        )
        if explicit_command:
            return request, persistent_agent_name, explicit_command.task, explicit_command

    if mention_agent_id:
        builtin = get_builtin_subagent(mention_agent_id)
        if builtin is not None:
            mentioned = next(
                (item for item in available_delegates if item.get("agent_id") == mention_agent_id),
                None,
            )
            if mentioned is None:
                raise HTTPException(status_code=403, detail="无法访问 @ 指定的子智能体")
        else:
            try:
                mentioned = service.get_by_id(mention_agent_id, user_id=user_id)
            except (LookupError, PermissionError) as exc:
                raise HTTPException(status_code=403, detail="无法访问 @ 指定的子智能体") from exc
        if mention_agent_name:
            selected_display_name = mention_agent_name
            mention_agent_name = str(mentioned["name"])
            execution_message = _strip_direct_mention_prefix(
                request.message,
                selected_display_name,
            )
        else:
            from core.services.subagent_routing_service import parse_explicit_subagent_command

            command = parse_explicit_subagent_command(request.message, [mentioned])
            if command:
                execution_message = command.task
    elif mention_agent_name:
        exact_matches = [
            item
            for item in available_delegates
            if item.get("name") == mention_agent_name and item.get("is_enabled", True)
        ]
        if not exact_matches:
            raise HTTPException(status_code=403, detail="无法访问 @ 指定的子智能体")
        if len(exact_matches) > 1:
            raise HTTPException(
                status_code=409,
                detail="存在同名子智能体，请重新从 @ 列表选择以确定目标",
            )
        mention_agent_id = str(exact_matches[0]["agent_id"])
        execution_message = _strip_direct_mention_prefix(
            request.message,
            mention_agent_name,
        )

    if mention_agent_id != request.mention_agent_id or (
        mention_agent_name and mention_agent_name != request.mention_name
    ):
        request = request.model_copy(
            update={
                "mention_agent_id": mention_agent_id,
                "mention_name": mention_agent_name,
            }
        )

    return request, persistent_agent_name, execution_message, explicit_command


def _strip_direct_mention_prefix(message: str, mention_name: Optional[str]) -> str:
    """Remove the frontend's display-only ``@name`` prefix before execution."""
    if not mention_name:
        return message
    token = f"@{mention_name}"
    if message == token:
        return message
    if message.startswith(token) and len(message) > len(token):
        separator = message[len(token)]
        if separator.isspace():
            stripped = message[len(token) :].lstrip()
            return stripped or message
    return message


def _build_user_extra_data(
    request: ChatRequest,
    model_provider_id: Optional[str] = None,
) -> Dict[str, Any]:
    extra: Dict[str, Any] = {"timestamp": now_iso()}
    if model_provider_id:
        extra["model_provider_id"] = model_provider_id
    if request.attachments:
        upload_meta = [
            {
                "name": a.name,
                "mime_type": a.mime_type,
                "file_id": a.file_id,
                "download_url": f"/files/{a.file_id}",
            }
            for a in request.attachments
            if a.file_id
        ]
        if upload_meta:
            extra["attachments"] = upload_meta
    if request.quoted_follow_up:
        extra["quoted_follow_up"] = request.quoted_follow_up.model_dump()
    if request.skill_id:
        extra["skill_id"] = request.skill_id
    if request.skill_name:
        extra["skill_name"] = request.skill_name
    if request.skill_ids:
        extra["skill_ids"] = request.skill_ids
    if request.mcp_ids:
        extra["mcp_ids"] = request.mcp_ids
    if request.plugin_name:
        extra["plugin_name"] = request.plugin_name
    if request.mention_name:
        extra["mention_name"] = request.mention_name
    if request.mention_agent_id:
        extra["mention_agent_id"] = request.mention_agent_id
    return extra


# _build_effective_user_message has been moved down into core/chat/context.py
# (shared with compaction_service / batch.py); this module imports it under the old name at the top.


# Cross-turn historical-file collection lives in ``core.chat.context`` so the
# channel inbound path (``core/channels/inbound.py``) can reuse the exact same
# logic — otherwise channel conversations never re-surface prior file_ids to the
# model and it hallucinates non-existent ids. Re-exported here under the old
# private names for backward compatibility (call site + tests).
from core.chat.context import _extract_message_file_ids
from core.chat.context import (  # noqa: E402
    collect_historical_attachments as _collect_historical_attachments,
)


def _build_ctx(
    request: ChatRequest,
    db_user_id: str,
    enabled_skills,
    enabled_agents,
    enabled_mcps,
    memory_enabled=False,
    memory_write_enabled=False,
    reranker_enabled=False,
    model_provider_id: Optional[str] = None,
    actual_model_name: Optional[str] = None,
    ontology_enabled: bool = False,
    ontology_pack_ids: Optional[List[str]] = None,
):
    # Explicitly referenced plugins: force-enable the plugin's MCP server into this turn's tool set ("enabled means usable").
    # enabled_mcps being None means using the catalog defaults (which already include enabled owned MCPs), so no forced narrowing is needed.
    if request.mcp_ids and enabled_mcps is not None:
        enabled_mcps = sorted(set(enabled_mcps) | {m for m in request.mcp_ids if m})
    current_attachments = (
        [a.model_dump() for a in request.attachments] if request.attachments else []
    )
    current_file_ids = {a.get("file_id") for a in current_attachments if a.get("file_id")}

    historical_files = _collect_historical_attachments(
        chat_id=request.chat_id,
        user_id=db_user_id,
        exclude_file_ids=current_file_ids,
    )

    # When chat_mode is not explicitly given, default to "thinking: medium"
    resolved_chat_mode = request.chat_mode or "medium"
    # Project metadata is edition-owned; the shared chat path consumes only the
    # returned context map and never imports organization models.
    project_id = getattr(request, "project_id", None)
    project_ctx: Dict[str, Any] = {
        "project_id": project_id,
        "project_name": None,
        "project_instructions": None,
        "project_folder_name": None,
        "project_folder_kind": None,
        "project_folder_id": None,
        "project_files": None,
    }
    if project_id:
        try:
            from core.db.engine import SessionLocal as _Sess
            from core.services.project_scope import build_project_ctx

            with _Sess() as _db:
                resolved_project_ctx = build_project_ctx(_db, project_id)
                if resolved_project_ctx:
                    project_ctx.update(resolved_project_ctx)
                    memory_enabled = bool(project_ctx.pop("_memory_enabled", True))
                    memory_write_enabled = bool(project_ctx.pop("_memory_write_enabled", True))
        except Exception:
            logger.warning("[chat] project ctx lookup failed for %s", project_id, exc_info=True)

    workspace_id_value = f"project:{project_id}" if project_id else "default"
    memory_scope_user_id_value = project_ctx.pop("memory_scope_user_id", None)

    from core.services.ontology_service import (
        build_ontology_runtime_for_preference,
        disabled_ontology_runtime,
    )

    ontology_runtime: Dict[str, Any] = disabled_ontology_runtime()
    if ontology_enabled:
        try:
            with SessionLocal() as _ontology_db:
                from core.services.ontology_policy import user_can_use_ontology_validation

                if user_can_use_ontology_validation(_ontology_db, db_user_id):
                    ontology_enabled, ontology_runtime = build_ontology_runtime_for_preference(
                        enabled=True,
                        task=request.message,
                        db=_ontology_db,
                        pack_ids=ontology_pack_ids or None,
                    )
                else:
                    ontology_enabled = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ontology] failed to build runtime policy")
            raise ServiceUnavailableError(
                "本体校验已开启，但运行时策略暂时不可用；为避免绕过校验，本次请求已停止"
            ) from exc

    ctx: Dict[str, Any] = {
        "model_name": actual_model_name or request.model_name,
        "model_provider_id": model_provider_id or "",
        "user_id": db_user_id,
        "chat_id": request.chat_id,
        "workspace_id": workspace_id_value,
        "memory_scope_user_id": memory_scope_user_id_value,
        "enable_thinking": resolved_chat_mode not in ("fast", "turbo"),
        "chat_mode": resolved_chat_mode,
        "mode_slug": request.mode_slug or "",
        "uploaded_files": current_attachments,
        "historical_files": historical_files,
        "memory_enabled": memory_enabled,
        "memory_write_enabled": memory_write_enabled,
        "reranker_enabled": reranker_enabled,
        "ontology_enabled": ontology_enabled,
        "ontology_runtime": ontology_runtime,
        # Preserve None so downstream (SkillsMiddleware) falls back to catalog defaults.
        # Only call _clean_id_list when there's an actual list to normalize.
        "enabled_skills": _clean_id_list(enabled_skills) if enabled_skills is not None else None,
        "enabled_agents": _clean_id_list(enabled_agents) if enabled_agents is not None else None,
        "enabled_mcps": _clean_id_list(enabled_mcps) if enabled_mcps is not None else None,
        "enabled_kbs": (
            _clean_id_list(request.enabled_kbs) if request.enabled_kbs is not None else None
        ),
        "agent_id": request.agent_id,
        "mention_agent_id": request.mention_agent_id,
        # A persistent child-agent conversation is the only direct route.
        # Per-turn @mentions stay on the normal main-model stream so the model
        # emits a real call_subagent tool call (with thinking and token deltas).
        "direct_agent_id": request.agent_id,
        "direct_agent_source": "dedicated_chat" if request.agent_id else None,
        "skill_id": request.skill_id,
        "skill_ids": request.skill_ids,
        "mcp_ids": request.mcp_ids,
        "plugin_name": request.plugin_name,
        "plan_chat": request.plan_chat,
        "batch_chat": request.batch_chat,
        "workflow_chat": request.workflow_chat,
        "disable_batch_plan": request.disable_batch_plan,
        **project_ctx,
    }
    return ctx


def _ensure_chat_session(
    chat_service: ChatService,
    chat_id: str,
    user_id: str,
    first_message: str,
    agent_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    plan_chat: bool = False,
    batch_chat: bool = False,
    workflow_chat: bool = False,
    project_id: Optional[str] = None,
):
    extra_data: Dict[str, Any] = {"chat_id": chat_id}
    if agent_id:
        extra_data["agent_id"] = agent_id
        if agent_name:
            extra_data["agent_name"] = agent_name
    if plan_chat:
        extra_data["plan_chat"] = True
    if batch_chat:
        extra_data["batch_chat"] = True
    if workflow_chat:
        extra_data["workflow_chat"] = True
    # Prefer the edition-aware access resolver before creating a session.
    pair = chat_service.get_session_with_access(chat_id, user_id)
    if pair is not None:
        session, level = pair
        if level not in ("admin", "edit"):
            # Read-only access levels are not allowed to write.
            raise HTTPException(status_code=403, detail="只读共享会话不可写入消息")
        if level != "admin":
            # Non-owner member: reuse the session, but never modify the metadata / project_id set by the owner
            return session
        # Owner path: fall through to ensure_session below (includes metadata merge / project attachment)
    # Project attachment (first write only, no cross-project drift) is handled uniformly by ensure_session.
    session = chat_service.ensure_session(
        chat_id=chat_id,
        user_id=user_id,
        title=generate_smart_title(first_message),
        extra_data=extra_data,
        project_id=project_id,
    )
    if session is None:
        raise HTTPException(status_code=403, detail="会话归属校验失败，无法访问该会话。")
    # Merge missing metadata flags into existing session
    existing_meta = session.extra_data or {}
    merged = dict(existing_meta)
    dirty = False
    if agent_id and not existing_meta.get("agent_id"):
        merged["agent_id"] = agent_id
        if agent_name:
            merged["agent_name"] = agent_name
        dirty = True
    if plan_chat and not existing_meta.get("plan_chat"):
        merged["plan_chat"] = True
        dirty = True
    if batch_chat and not existing_meta.get("batch_chat"):
        merged["batch_chat"] = True
        dirty = True
    if workflow_chat and not existing_meta.get("workflow_chat"):
        merged["workflow_chat"] = True
        dirty = True
    if dirty:
        chat_service.update_session(chat_id, user_id, {"extra_data": merged})
    return session


def _load_session_messages(
    chat_service: ChatService, chat_id: str, user_id: str
) -> List[Dict[str, Any]]:
    # Checkpoint-aware history loading: when a checkpoint exists, fetch only the tail messages from the DB (no more load-everything-then-discard).
    # Cross-turn tool results are not truncated. See core/services/compaction_service.py.
    from core.services.compaction_service import load_session_history

    messages = load_session_history(chat_service, chat_id, user_id)
    if messages is None:
        raise HTTPException(status_code=404, detail=f"Session {chat_id} not found")
    return messages


@router.post("/send", response_model=ChatResponse, summary="非流式聊天")
async def chat_send(
    request: ChatRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """非流式聊天：一次性返回完整回复（不走 SSE）。

    校验主对话模型已配置、会话归属及项目权限后，把用户消息落库并同步运行
    工作流，附带追问问题。需认证：有效 session cookie 或有效 API-Key，二者皆无返回 401。
    """
    _ensure_main_model_configured()
    chat_service = ChatService(db)
    db_user_id = resolve_db_user_id(db, _authenticated_user_id(user))
    request, _agent_name, execution_message, explicit_subagent_command = (
        _resolve_chat_agent_targets(
            db,
            request,
            db_user_id,
        )
    )
    effective_user_message = _build_effective_user_message(
        execution_message, request.quoted_follow_up
    )
    selected_model_provider_id = _resolve_selected_model_provider_id(db, request, db_user_id)
    actual_model_name = _resolve_actual_chat_model_name(request, selected_model_provider_id)
    enabled_skills, enabled_agents, enabled_mcps = resolve_enabled_capabilities(
        db,
        db_user_id,
        request.enabled_skills,
        request.enabled_agents,
        request.enabled_mcps,
    )

    # Project attachment check: if the project is inaccessible, return 404
    if request.project_id:
        from core.auth.permissions_iface import resolve_project_permission
        from core.db.models import Project as _Project

        _proj = (
            db.query(_Project)
            .filter(
                _Project.project_id == request.project_id,
                _Project.deleted_at.is_(None),
            )
            .first()
        )
        if _proj is None or resolve_project_permission(db, db_user_id, _proj) == "none":
            raise HTTPException(status_code=404, detail="项目不存在或你无权访问")

    accepted = None
    try:
        _ensure_chat_session(
            chat_service,
            request.chat_id,
            db_user_id,
            request.message,
            agent_id=request.agent_id,
            agent_name=_agent_name,
            plan_chat=request.plan_chat,
            batch_chat=request.batch_chat,
            workflow_chat=request.workflow_chat,
            project_id=request.project_id,
        )
        # Link orphan artifacts (uploaded before session existed) to this chat
        if request.attachments:
            _att_ids = [a.file_id for a in request.attachments if a.file_id]
            if _att_ids:
                from core.db.models import Artifact as _ArtModel

                db.query(_ArtModel).filter(
                    _ArtModel.artifact_id.in_(_att_ids),
                    _ArtModel.user_id == db_user_id,
                    _ArtModel.chat_id.is_(None),
                ).update({"chat_id": request.chat_id}, synchronize_session="fetch")
                db.commit()
        from core.services.chat_sequencer import ChatBusyError, ChatSequencer

        request_payload = request.model_dump(exclude_none=True)
        if explicit_subagent_command:
            request_payload["explicit_subagent_command"] = {
                "agent_id": explicit_subagent_command.agent_id,
                "agent_name": explicit_subagent_command.agent_name,
                "task": explicit_subagent_command.task,
            }
        if selected_model_provider_id:
            request_payload["model_provider_id"] = selected_model_provider_id
        else:
            request_payload.pop("model_provider_id", None)
        try:
            accepted = ChatSequencer(db).accept_main_run(
                chat_id=request.chat_id,
                user_id=db_user_id,
                user_content=request.message,
                request_payload=request_payload,
                model=actual_model_name,
                user_extra_data=_build_user_extra_data(request, selected_model_provider_id),
            )
        except ChatBusyError as exc:
            raise chat_busy_http_exception(exc) from exc

        session_messages = _load_session_messages(chat_service, request.chat_id, db_user_id)

        _user_settings = UserService(db).get_user_settings(db_user_id)
        ctx = _build_ctx(
            request,
            db_user_id,
            enabled_skills,
            enabled_agents,
            enabled_mcps,
            model_provider_id=selected_model_provider_id,
            actual_model_name=actual_model_name,
            ontology_enabled=bool(_user_settings.get("ontology_enabled", False)),
            ontology_pack_ids=_user_settings.get("ontology_pack_ids") or None,
        )
        if explicit_subagent_command:
            ctx["explicit_subagent_command"] = {
                "agent_id": explicit_subagent_command.agent_id,
                "agent_name": explicit_subagent_command.agent_name,
                "task": explicit_subagent_command.task,
            }

        from orchestration import chat_run_executor

        run = await chat_run_executor.start_run(
            accepted_run=accepted.run,
            chat_id=request.chat_id,
            user_id=db_user_id,
            session_messages=session_messages,
            effective_user_message=effective_user_message,
            raw_user_message=request.message,
            context=ctx,
            model_name=actual_model_name,
        )
        terminal_run = await chat_run_executor.wait_run(run.run_id)
        if terminal_run.status == "cancelled":
            raise HTTPException(
                status_code=409,
                detail={"code": "run_cancelled", "run_id": run.run_id},
            )
        if terminal_run.status != "completed":
            raise RuntimeError(terminal_run.error_message or "chat run failed")

        db.expire_all()
        assistant = chat_service.get_message_by_id(run.message_id)
        if assistant is None:
            raise RuntimeError("completed chat run has no assistant message")
        meta = assistant.extra_data or {}

        return ChatResponse(
            chat_id=request.chat_id,
            response=assistant.content,
            timestamp=now_iso(),
            is_markdown=bool(meta.get("is_markdown", False)),
            route=meta.get("route", "main"),
            sources=meta.get("sources", []),
            artifacts=meta.get("artifacts", []),
            warnings=meta.get("warnings", []),
        )
    except HTTPException:
        if accepted is not None:
            from core.services.chat_sequencer import ChatSequencer

            ChatSequencer(db).fail_run(accepted.run.run_id, reason="request preparation failed")
        raise
    except Exception as e:
        if accepted is not None:
            from core.services.chat_sequencer import ChatSequencer

            ChatSequencer(db).fail_run(accepted.run.run_id, reason=str(e))
        logger.error("chat_send_failed", chat_id=request.chat_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=resolve_user_facing_error(e))


def _release_request_session(db: Session) -> None:
    """End the request transaction before an SSE response is handed over.

    A streaming response outlives its handler: FastAPI keeps the request-scoped
    session (``Depends(get_db)``) open until the stream completes. Anything left
    open here is therefore a transaction — and any row lock in it — held for the
    entire turn. The background run then blocks on its own chat row when it
    commits the reply, so the run cannot finish, so the stream cannot finish, so
    this session is never released: a circular wait that only a client
    disconnect breaks.

    Committing rather than rolling back matches what the handler already did on
    every other exit: everything it meant to persist is committed by this point,
    so this only ends the read transaction the history load left behind.
    """
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - never turn cleanup into a failed request
        logger.warning("release_request_session_commit_failed", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("release_request_session_rollback_failed", exc_info=True)
    finally:
        db.close()


@router.post("/stream", summary="流式聊天 (SSE)")
async def chat_stream(
    request: ChatRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """流式聊天：以 SSE 持续推送 text / tool_call / tool_result / meta / done 事件。

    校验模型与会话权限后启动后台 run，再以 SSE 跟随该 run 实时下发；断线可用
    /stream/{run_id} 续播。需认证：有效 session cookie 或有效 API-Key，二者皆无返回 401。
    """
    _ensure_main_model_configured()
    chat_service = ChatService(db)
    db_user_id = resolve_db_user_id(db, _authenticated_user_id(user))
    request, _agent_name_stream, execution_message, explicit_subagent_command = (
        _resolve_chat_agent_targets(
            db,
            request,
            db_user_id,
        )
    )
    effective_user_message = _build_effective_user_message(
        execution_message, request.quoted_follow_up
    )
    selected_model_provider_id = _resolve_selected_model_provider_id(db, request, db_user_id)
    actual_model_name = _resolve_actual_chat_model_name(request, selected_model_provider_id)

    # ── Parallelize 2 read-only DB queries via to_thread + independent
    # sessions. Previously these ran serially in the request thread
    # (capabilities + user settings), now
    # bounded by the slowest single query. SQLAlchemy Session is not
    # thread-safe → each task opens its own SessionLocal().
    _req_skills = request.enabled_skills
    _req_agents = request.enabled_agents
    _req_mcps = request.enabled_mcps

    def _read_caps():
        with SessionLocal() as _db:
            return resolve_enabled_capabilities(
                _db,
                db_user_id,
                _req_skills,
                _req_agents,
                _req_mcps,
            )

    def _read_settings():
        with SessionLocal() as _db:
            return UserService(_db).get_user_settings(db_user_id)

    (enabled_skills, enabled_agents, enabled_mcps), _user_settings = await asyncio.gather(
        asyncio.to_thread(_read_caps),
        asyncio.to_thread(_read_settings),
    )

    _memory_enabled = bool(_user_settings.get("memory_enabled", False))
    _memory_write_enabled = bool(_user_settings.get("memory_write_enabled", False))
    _reranker_enabled = bool(_user_settings.get("reranker_enabled", False))

    # Project attachment check: if the project is inaccessible, return 404 (avoids writing an unknown project_id onto the session)
    if request.project_id:
        from core.auth.permissions_iface import resolve_project_permission
        from core.db.models import Project as _Project

        _proj = (
            db.query(_Project)
            .filter(
                _Project.project_id == request.project_id,
                _Project.deleted_at.is_(None),
            )
            .first()
        )
        if _proj is None or resolve_project_permission(db, db_user_id, _proj) == "none":
            raise HTTPException(status_code=404, detail="项目不存在或你无权访问")

    _ensure_chat_session(
        chat_service,
        request.chat_id,
        db_user_id,
        request.message,
        agent_id=request.agent_id,
        agent_name=_agent_name_stream,
        plan_chat=request.plan_chat,
        batch_chat=request.batch_chat,
        workflow_chat=request.workflow_chat,
        project_id=request.project_id,
    )

    context = _build_ctx(
        request,
        db_user_id,
        enabled_skills,
        enabled_agents,
        enabled_mcps,
        memory_enabled=_memory_enabled,
        memory_write_enabled=_memory_write_enabled,
        reranker_enabled=_reranker_enabled,
        model_provider_id=selected_model_provider_id,
        actual_model_name=actual_model_name,
        ontology_enabled=bool(_user_settings.get("ontology_enabled", False)),
        ontology_pack_ids=_user_settings.get("ontology_pack_ids") or None,
    )
    if explicit_subagent_command:
        context["explicit_subagent_command"] = {
            "agent_id": explicit_subagent_command.agent_id,
            "agent_name": explicit_subagent_command.agent_name,
            "task": explicit_subagent_command.task,
        }

    from orchestration import chat_run_executor

    request_payload = request.model_dump(exclude_none=True)
    if explicit_subagent_command:
        request_payload["explicit_subagent_command"] = {
            "agent_id": explicit_subagent_command.agent_id,
            "agent_name": explicit_subagent_command.agent_name,
            "task": explicit_subagent_command.task,
        }
    if selected_model_provider_id:
        request_payload["model_provider_id"] = selected_model_provider_id
    else:
        request_payload.pop("model_provider_id", None)

    # The user message + pending run are one durable admission transaction.
    # A concurrent loser receives the winner's resumable run and leaves no
    # orphan message or sequence gap behind.
    from core.services.chat_sequencer import ChatBusyError, ChatSequencer

    try:
        accepted = ChatSequencer(db).accept_main_run(
            chat_id=request.chat_id,
            user_id=db_user_id,
            user_content=request.message,
            model=actual_model_name,
            user_extra_data=_build_user_extra_data(request, selected_model_provider_id),
            request_payload=request_payload,
        )
    except ChatBusyError as exc:
        raise chat_busy_http_exception(exc) from exc

    # From this point the main-writer slot is held.  Load history only now so
    # it cannot miss the immediately preceding run that was still finishing
    # during the former parallel pre-read.
    try:
        session_messages = _load_session_messages(chat_service, request.chat_id, db_user_id)

        # 新一轮用户消息 = 上一段任务的计划栏作废（和前端发送时清空计划栏是同一条规则）。
        from core.chat.plan_progress import clear_plan_progress

        clear_plan_progress(request.chat_id)

        # Link orphan artifacts (uploaded before session existed) to this chat.
        if request.attachments:
            _att_ids = [a.file_id for a in request.attachments if a.file_id]
            if _att_ids:
                from core.db.models import Artifact as _ArtModel

                db.query(_ArtModel).filter(
                    _ArtModel.artifact_id.in_(_att_ids),
                    _ArtModel.user_id == db_user_id,
                    _ArtModel.chat_id.is_(None),
                ).update({"chat_id": request.chat_id}, synchronize_session="fetch")
                db.commit()

        run = await chat_run_executor.start_run(
            accepted_run=accepted.run,
            chat_id=request.chat_id,
            user_id=db_user_id,
            session_messages=session_messages,
            effective_user_message=effective_user_message,
            raw_user_message=request.message,
            context=context,
            model_name=actual_model_name,
        )
    except Exception as exc:
        ChatSequencer(db).abandon_pending_run(accepted.run.run_id, reason=str(exc))
        raise

    # Read every value off the ORM row *before* the session is released:
    # closing it detaches the instance, and a later attribute access would
    # raise DetachedInstanceError instead of returning the stream.
    started_run_id = run.run_id
    started_chat_id = request.chat_id
    _release_request_session(db)
    return sse_response(
        chat_run_executor.follow_run_as_sse(started_run_id, chat_id=started_chat_id),
    )


@router.get("/stream/{run_id}", summary="续播 run（用于刷新后重新订阅）")
async def chat_stream_resume(
    run_id: str,
    from_offset: int = Query(0, alias="from", ge=0, description="从此 offset 之后继续推送"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """续播某个进行中的 run：刷新或断线后从指定 offset 之后重新订阅 SSE 事件。

    校验 run 存在且归属当前用户（非属主 403、不存在 404），返回 text/event-stream。
    """
    from orchestration import chat_run_executor

    db_user_id = resolve_db_user_id(db, _authenticated_user_id(user))
    run = chat_run_executor.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != db_user_id:
        raise HTTPException(status_code=403, detail="无权访问该 run")
    resumed_chat_id = run.chat_id
    _release_request_session(db)
    return sse_response(
        chat_run_executor.follow_run_as_sse(
            run_id, chat_id=resumed_chat_id, from_offset=from_offset
        ),
    )


@router.get("/{chat_id}/active-run", summary="探测会话是否有进行中的 run")
async def chat_active_run(
    chat_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """探测会话当前是否有进行中的 run，供前端重连时决定是否续播。

    有则返回 run_id / message_id / status / 刷新重放 offset / thinking 模式等元信息，
    无则返回 data=null。需认证（有效 cookie 或 API-Key）。
    """
    from orchestration import chat_run_executor

    db_user_id = resolve_db_user_id(db, _authenticated_user_id(user))
    run = chat_run_executor.get_active_run_for_chat(chat_id, db_user_id)
    if run is None:
        return success_response(data=None)
    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    kind = payload.get("kind", "chat")
    plan_id = payload.get("plan_id")
    # Resume needs the run's thinking mode: the SSE replay parser must start in
    # the right phase. The model emits reasoning with the opening <think> tag
    # frequently absent, so a wrong initial phase flattens it into the answer.
    resolved_mode = payload.get("chat_mode") or (
        "medium" if payload.get("enable_thinking") else "fast"
    )
    # This endpoint is a fresh-client probe: the server does not persist how
    # much of the stream this particular browser consumed.  ``run.last_event_offset``
    # is the producer high-water mark, not a consumer resume cursor.  Returning
    # it here would make a refreshed client skip the already-produced prefix
    # immediately after it clears the partial assistant message from the UI.
    replay_from_offset = 0
    return success_response(
        data={
            "run_id": run.run_id,
            "message_id": run.message_id,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "last_event_offset": replay_from_offset,
            "kind": kind,
            "plan_id": plan_id,
            "enable_thinking": resolved_mode not in ("fast", "turbo"),
        }
    )


# ── Regenerate / Edit ────────────────────────────────────────────────────


def _restore_attachments(saved: List[Dict]) -> List[AttachmentItem]:
    """Reconstruct AttachmentItem list from extra_data['attachments'] metadata."""
    return [
        AttachmentItem(
            name=a.get("name", ""),
            mime_type=a.get("mime_type", ""),
            file_id=a.get("file_id", ""),
        )
        for a in saved
        if a.get("file_id")
    ]


async def _stream_sse_response(
    *,
    chat_service: ChatService,
    chat_id: str,
    model_name: str,
    session_messages: List[Dict[str, Any]],
    user_message: str,
    context: Dict[str, Any],
    user_content_for_followup: Optional[str] = None,
    error_label: str = "stream_failed",
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
):
    """Shared SSE generator for chat_stream, regenerate, and edit endpoints.

    Yields SSE-formatted strings. Handles tool_call, tool_result, thinking,
    content, heartbeat, and meta events. Persists the assistant message and
    optionally generates follow-up questions in the background.
    """
    try:
        from core.llm import workspace as _workspace_mod

        full_response = ""
        pending_message_id = f"msg_{uuid.uuid4().hex[:16]}"
        metadata: dict = {}
        tool_calls_log: list = []
        # 本轮回答总耗时起点 —— 持久化进 extra_data.duration_ms
        _stream_started_monotonic = time.monotonic()
        # 结构化 reasoning 思考增量：按到达顺序以 <think>…</think> 块交错并入
        # full_response 落库（与内联思考模型一致），刷新后思考过程可回放。
        _thinking_parts: list = []

        def _flush_thinking() -> None:
            nonlocal full_response
            if not _thinking_parts:
                return
            block = "".join(_thinking_parts)
            _thinking_parts.clear()
            close = "</think>"
            last_close = full_response.rfind(close)
            # 结构化 reasoning 的尾部增量可能在正文已开始后才到达（正文首 token
            # 已出、思考收尾的"。"后到）。实时侧把它并回前一个思考块
            # （appendThinkingContentBeforeTrailingText），落库遵循同一规则——
            # 否则 <think> 块会把正文句子从中间切开，刷新后与实时展示不一致。
            if last_close != -1 and full_response[last_close + len(close) :].strip():
                full_response = full_response[:last_close] + block + full_response[last_close:]
                # 中段插入使其后的坐标整体右移，已记录的 content_offset 同步平移
                for _tc in tool_calls_log:
                    off = _tc.get("content_offset")
                    if isinstance(off, int) and off > last_close:
                        _tc["content_offset"] = off + len(block)
                return
            full_response += "<think>" + block + close

        # Per-run workspace state — pin_to_workspace tool reads/writes this.
        _workspace_mod.init_state()

        async def _bound_chunks():
            from core.services.run_journal import durable_run_binding

            principal = str(user_id or context.get("user_id") or "")
            if not principal:
                raise RuntimeError("legacy chat stream requires an authenticated user")
            recovery_context = {
                **dict(context),
                "mcp_ids": context.get("mcp_ids") or context.get("enabled_mcps") or [],
                "skill_ids": context.get("skill_ids") or context.get("enabled_skills") or [],
                "kb_ids": context.get("kb_ids") or context.get("enabled_kbs") or [],
                "model_name": model_name,
            }
            async with durable_run_binding(
                user_id=principal,
                chat_id=chat_id,
                kind="legacy_chat_stream",
                external_id=pending_message_id,
                request_payload={"operation": error_label},
                recovery_snapshot={"worker_args": {"context": recovery_context}},
                session_factory=SessionLocal,
            ) as binding:
                bound_context = {
                    **dict(context),
                    "run_id": binding.run_id,
                    "journal_owner": binding.owner,
                }
                async for item in astream_chat_workflow(
                    session_messages=session_messages,
                    user_message=user_message,
                    context=bound_context,
                ):
                    yield item

        async for chunk in _bound_chunks():
            chunk_type = chunk.get("type")
            if chunk_type == "thinking":
                _thinking_evt = build_thinking_event(chunk, chat_id)
                if _thinking_evt.get("delta"):
                    _thinking_parts.append(str(_thinking_evt["delta"]))
                yield f"data: {json.dumps(_thinking_evt, ensure_ascii=False)}\n\n"
            elif chunk_type in {"ai_message", "content"}:
                delta = chunk.get("delta", "")
                if delta:
                    _flush_thinking()
                    full_response += delta
                    yield f"data: {json.dumps({'type': 'content', 'event': 'ai_message', 'delta': delta, 'chat_id': chat_id}, ensure_ascii=False)}\n\n"
            elif chunk_type == "content_replace":
                _thinking_parts.clear()
                full_response = str(chunk.get("content") or "")
                # 整体替换后，先前记录的 content_offset 指向旧草稿坐标系，
                # 已无意义——清掉，让历史重建走「合并正文」兜底而不是错切。
                for _tc in tool_calls_log:
                    _tc.pop("content_offset", None)
                event = {**chunk, "chat_id": chat_id}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            elif chunk_type == "tool_call":
                _flush_thinking()
                _tc_evt = build_tool_call_event(chunk, chat_id, tool_calls_log)
                # 记录该工具卡片出现时正文的累计长度（含 <think> 标记，与持久化
                # content 同一坐标系）：历史重建按此偏移把「文本 ↔ 工具卡片」按
                # 流式原顺序交错（问题15：刷新后内容与实时不一致）。
                for _tc in tool_calls_log:
                    _tc.setdefault("content_offset", len(full_response))
                yield f"data: {json.dumps(_tc_evt, ensure_ascii=False)}\n\n"
            elif chunk_type == "tool_call_start":
                _flush_thinking()
                _ts_evt = build_tool_call_start_event(chunk, chat_id)
                yield f"data: {json.dumps(_ts_evt, ensure_ascii=False)}\n\n"
            elif chunk_type == "tool_call_delta":
                _td_evt = build_tool_call_delta_event(chunk, chat_id)
                yield f"data: {json.dumps(_td_evt, ensure_ascii=False)}\n\n"
            elif chunk_type == "tool_result":
                _tr_evt = build_tool_result_event(chunk, chat_id, tool_calls_log)
                # attach_tool_result 可能刚补录了一个没有 tool_call 事件的条目：
                # 此刻就补记 offset，否则它要等下一个 tool_call 才拿到（晚一个
                # 叙述段），最后一个工具则永远缺失 → 整条消息退化成堆叠展示。
                for _tc in tool_calls_log:
                    _tc.setdefault("content_offset", len(full_response))
                yield f"data: {json.dumps(_tr_evt, ensure_ascii=False)}\n\n"
            elif chunk_type == "heartbeat":
                yield ": heartbeat\n\n"
            elif chunk_type == "tool_pending":
                _tp_evt = {
                    "type": "tool_pending",
                    "chat_id": chat_id,
                    "reason": chunk.get("reason", "llm_buffering"),
                }
                if chunk.get("scope"):
                    _tp_evt["scope"] = chunk["scope"]
                yield f"data: {json.dumps(_tp_evt, ensure_ascii=False)}\n\n"
            elif chunk_type in ("batch_confirm", "file_confirm", "design_pick"):
                # Confirmation-type events are passed through whole: batch-execution confirm /
                # §13 MySpace write confirm / site-design pick-one-of-three. If the regenerate /
                # edit-and-resend paths trigger these suspensions, they must be forwarded on this
                # stream too — otherwise the confirmation card never appears and the tool idles
                # until timeout. To add a new confirmation-type event, just add its type to the set above.
                _cf_evt = {
                    **{k: v for k, v in chunk.items() if k != "type"},
                    "type": chunk_type,
                    "chat_id": chat_id,
                }
                yield f"data: {json.dumps(_cf_evt, ensure_ascii=False)}\n\n"
            elif chunk_type == "user_question":
                _question_evt = build_user_question_event(chunk, chat_id)
                yield f"data: {json.dumps(_question_evt, ensure_ascii=False)}\n\n"
            elif chunk_type == "user_question_resolved":
                _resolved_evt = build_user_question_resolved_event(chunk, chat_id)
                yield f"data: {json.dumps(_resolved_evt, ensure_ascii=False)}\n\n"
            elif chunk_type in {
                "ontology_activation",
                "ontology_gate",
                "ontology_review",
                "ontology_repair",
                "ontology_revision",
                "ontology_revision_thinking",
            }:
                event = {**chunk, "chat_id": chat_id}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            elif chunk_type == "meta":
                _flush_thinking()
                pending_message_id = f"msg_{uuid.uuid4().hex[:16]}"
                # Strict workspace gate: the agent's pin_to_workspace calls
                # are the SOLE source of user-visible artifacts. We always
                # emit workspace_files (possibly empty) so the frontend can
                # distinguish "new strict-mode message" (array, possibly
                # empty → show only allowlisted) from "legacy message
                # without this field" (undefined → show everything).
                _ws_pinned = _workspace_mod.get_pinned()
                _ws_files: List[str] = _workspace_mod.get_pinned_file_ids()
                metadata = {
                    "type": "meta",
                    "route": chunk.get("route", "main"),
                    "sources": chunk.get("sources", []),
                    "artifacts": _ws_pinned,
                    "warnings": chunk.get("warnings", []),
                    "is_markdown": chunk.get("is_markdown", False),
                    "chat_id": chat_id,
                    "message_id": pending_message_id,
                    "citations": chunk.get("citations", []),
                    "workspace_files": _ws_files,
                    "ontology_governance": chunk.get("ontology_governance"),
                    # Skeleton marker: settlement runs after the stream closes.
                    "evolution_pending": chunk.get("evolution_pending"),
                }
                yield f"data: {json.dumps(metadata, ensure_ascii=False)}\n\n"

                _usage = chunk.get("usage") or None
                _persist_extra: dict = {
                    "timestamp": now_iso(),
                    "route": metadata.get("route"),
                    "is_markdown": metadata.get("is_markdown", False),
                    "sources": metadata.get("sources", []),
                    "artifacts": metadata.get("artifacts", []),
                    "warnings": metadata.get("warnings", []),
                    "citations": metadata.get("citations", []),
                    "workspace_files": metadata.get("workspace_files", []),
                    "duration_ms": int((time.monotonic() - _stream_started_monotonic) * 1000),
                }
                if metadata.get("ontology_governance"):
                    _persist_extra["ontology_governance"] = metadata["ontology_governance"]
                if context.get("model_provider_id"):
                    _persist_extra["model_provider_id"] = context.get("model_provider_id")
                chat_service.add_message(
                    chat_id=chat_id,
                    role="assistant",
                    content=full_response,
                    model=model_name,
                    tool_calls=tool_calls_log if tool_calls_log else None,
                    usage=_usage,
                    message_id=pending_message_id,
                    extra_data={
                        **_persist_extra,
                        "message_id": pending_message_id,
                    },
                )
                if db and user_id:
                    # Build a ProjectScope from the workflow context and pass it explicitly.
                    # This is the core fix for the personal MySpace root leak in trace 9d218075…:
                    # workflow.py's finally block has already cleared the internal scope by this
                    # point, so it must be passed explicitly to keep generated output
                    # inside the project-owned file scope.
                    # with user_folder_id=NULL.
                    _stream_scope = project_scope_from_context(context)
                    _persist_artifacts(
                        db,
                        user_id,
                        chat_id,
                        _ws_pinned,
                        scope=_stream_scope,
                    )

                yield "data: [DONE]\n\n"

                # Background follow-up generation
                _fup_user = user_content_for_followup or user_message
                _fup_resp = full_response
                _fup_id = pending_message_id

                async def _generate_followups_bg(u: str, r: str, mid: str):
                    try:
                        clean = strip_thinking(r)
                        questions = await asyncio.wait_for(
                            get_followup_generator().generate(u, clean),
                            timeout=10,
                        )
                        if questions:
                            from core.db.engine import SessionLocal

                            with SessionLocal() as bg_db:
                                ChatService(bg_db).update_message_extra_data(
                                    mid, {"follow_up_questions": questions}
                                )
                    except Exception as exc:
                        logger.warning("background follow_up generation failed: %r", exc)

                asyncio.create_task(_generate_followups_bg(_fup_user, _fup_resp, _fup_id))

    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "请求处理失败，请稍后重试"
        yield f"data: {json.dumps({'type': 'error', 'error': detail, 'chat_id': chat_id}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        logger.error(error_label, chat_id=chat_id, error=str(e), exc_info=True)
        user_facing = resolve_user_facing_error(e)
        yield f"data: {json.dumps({'type': 'error', 'error': user_facing, 'chat_id': chat_id}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


class RegenerateRequest(BaseModel):
    """Request body for regenerating an assistant response."""

    message_index: int = Field(
        ..., description="0-based index of the assistant message in the chat"
    )


class EditAndResendRequest(BaseModel):
    """Request body for editing a user message and regenerating."""

    message_index: int = Field(..., description="0-based index of the user message in the chat")
    new_content: str = Field(
        ..., min_length=1, max_length=10000, description="New content for the user message"
    )


@router.post("/{chat_id}/regenerate", summary="重新生成助手回复 (SSE)")
async def regenerate_message(
    chat_id: str,
    body: RegenerateRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete the target assistant message and all subsequent, then re-stream."""
    _ensure_main_model_configured()
    chat_service = ChatService(db)
    db_user_id = resolve_db_user_id(db, _authenticated_user_id(user))

    # Shared sessions: deleting/rewriting history is only granted to the session owner / project admin (the "admin" level in the shared context).
    pair = chat_service.get_session_with_access(chat_id, db_user_id)
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    _sess, _level = pair
    if _level != "admin":
        raise HTTPException(status_code=403, detail="共享会话仅创建者可重新生成回复")

    target_msg = chat_service.get_message_by_index(chat_id, body.message_index)
    if not target_msg or target_msg.chat_id != chat_id:
        raise HTTPException(status_code=404, detail="消息不存在")

    user_msg = chat_service.get_user_message_before(chat_id, target_msg.message_id)
    if not user_msg:
        raise HTTPException(status_code=400, detail="找不到对应的用户消息")

    user_content = user_msg.content
    user_extra = user_msg.extra_data or {}
    attachment_items = _restore_attachments(user_extra.get("attachments", []))

    regen_request = ChatRequest(
        chat_id=chat_id,
        message=user_content,
        model_name="qwen",
        enable_thinking=user_extra.get("enable_thinking", False),
        quoted_follow_up=user_extra.get("quoted_follow_up"),
        attachments=attachment_items,
        model_provider_id=user_extra.get("model_provider_id"),
    )
    selected_model_provider_id = _resolve_selected_model_provider_id(db, regen_request, db_user_id)
    actual_model_name = _resolve_actual_chat_model_name(
        regen_request,
        selected_model_provider_id,
    )
    enabled_skills, enabled_agents, enabled_mcps = resolve_enabled_capabilities(db, db_user_id)
    _user_settings = UserService(db).get_user_settings(db_user_id)
    effective_msg = _build_effective_user_message(
        regen_request.message, regen_request.quoted_follow_up
    )

    context = _build_ctx(
        regen_request,
        db_user_id,
        enabled_skills,
        enabled_agents,
        enabled_mcps,
        memory_enabled=bool(_user_settings.get("memory_enabled", False)),
        memory_write_enabled=bool(_user_settings.get("memory_write_enabled", False)),
        reranker_enabled=bool(_user_settings.get("reranker_enabled", False)),
        model_provider_id=selected_model_provider_id,
        actual_model_name=actual_model_name,
        ontology_enabled=bool(_user_settings.get("ontology_enabled", False)),
        ontology_pack_ids=_user_settings.get("ontology_pack_ids") or None,
    )

    from core.services.chat_sequencer import ChatBusyError, ChatSequencer
    from orchestration import chat_run_executor

    request_payload = regen_request.model_dump(exclude_none=True)
    request_payload["operation"] = "regenerate"
    try:
        accepted = ChatSequencer(db).accept_existing_user_run(
            chat_id=chat_id,
            user_id=db_user_id,
            user_message_id=user_msg.message_id,
            delete_from_message_id=target_msg.message_id,
            delete_from_chat_seq=target_msg.chat_seq,
            request_payload=request_payload,
        )
    except ChatBusyError as exc:
        raise chat_busy_http_exception(exc) from exc

    try:
        session_messages = _load_session_messages(chat_service, chat_id, db_user_id)
        run = await chat_run_executor.start_run(
            accepted_run=accepted.run,
            chat_id=chat_id,
            user_id=db_user_id,
            session_messages=session_messages,
            effective_user_message=effective_msg,
            raw_user_message=user_content,
            context=context,
            model_name=actual_model_name,
        )
    except Exception as exc:
        ChatSequencer(db).abandon_pending_run(accepted.run.run_id, reason=str(exc))
        raise

    started_run_id = run.run_id
    _release_request_session(db)
    return sse_response(
        chat_run_executor.follow_run_as_sse(started_run_id, chat_id=chat_id),
    )


@router.post("/{chat_id}/edit", summary="编辑消息并重新生成 (SSE)")
async def edit_and_resend(
    chat_id: str,
    body: EditAndResendRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete the target user message and all subsequent, then re-stream with new content."""
    _ensure_main_model_configured()
    chat_service = ChatService(db)
    db_user_id = resolve_db_user_id(db, _authenticated_user_id(user))

    # Shared sessions: editing history is an owner-only operation (rewriting others' / one's own earlier messages would break the collaboration context).
    pair = chat_service.get_session_with_access(chat_id, db_user_id)
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    _sess, _level = pair
    if _level != "admin":
        raise HTTPException(status_code=403, detail="共享会话仅创建者可编辑历史消息")

    target_msg = chat_service.get_message_by_index(chat_id, body.message_index)
    if not target_msg or target_msg.chat_id != chat_id or target_msg.role != "user":
        raise HTTPException(status_code=404, detail="用户消息不存在")

    target_extra = target_msg.extra_data or {}
    saved_attachments = target_extra.get("attachments", [])
    attachment_items = _restore_attachments(saved_attachments)

    edit_request = ChatRequest(
        chat_id=chat_id,
        message=body.new_content,
        model_name="qwen",
        attachments=attachment_items,
        model_provider_id=target_extra.get("model_provider_id"),
    )
    selected_model_provider_id = _resolve_selected_model_provider_id(db, edit_request, db_user_id)
    actual_model_name = _resolve_actual_chat_model_name(edit_request, selected_model_provider_id)
    enabled_skills, enabled_agents, enabled_mcps = resolve_enabled_capabilities(db, db_user_id)
    _user_settings = UserService(db).get_user_settings(db_user_id)

    # Persist the edited user message
    _edit_extra: Dict[str, Any] = {"timestamp": now_iso()}
    if saved_attachments:
        _edit_extra["attachments"] = saved_attachments
    if selected_model_provider_id:
        _edit_extra["model_provider_id"] = selected_model_provider_id

    context = _build_ctx(
        edit_request,
        db_user_id,
        enabled_skills,
        enabled_agents,
        enabled_mcps,
        memory_enabled=bool(_user_settings.get("memory_enabled", False)),
        memory_write_enabled=bool(_user_settings.get("memory_write_enabled", False)),
        reranker_enabled=bool(_user_settings.get("reranker_enabled", False)),
        model_provider_id=selected_model_provider_id,
        actual_model_name=actual_model_name,
        ontology_enabled=bool(_user_settings.get("ontology_enabled", False)),
        ontology_pack_ids=_user_settings.get("ontology_pack_ids") or None,
    )

    from core.services.chat_sequencer import ChatBusyError, ChatSequencer
    from orchestration import chat_run_executor

    request_payload = edit_request.model_dump(exclude_none=True)
    request_payload["operation"] = "edit"
    try:
        accepted = ChatSequencer(db).accept_replacement_user_run(
            chat_id=chat_id,
            user_id=db_user_id,
            target_user_message_id=target_msg.message_id,
            delete_from_chat_seq=target_msg.chat_seq,
            user_content=body.new_content,
            request_payload=request_payload,
            model=actual_model_name,
            user_extra_data=_edit_extra,
        )
    except ChatBusyError as exc:
        raise chat_busy_http_exception(exc) from exc

    try:
        session_messages = _load_session_messages(chat_service, chat_id, db_user_id)
        run = await chat_run_executor.start_run(
            accepted_run=accepted.run,
            chat_id=chat_id,
            user_id=db_user_id,
            session_messages=session_messages,
            effective_user_message=body.new_content,
            raw_user_message=body.new_content,
            context=context,
            model_name=actual_model_name,
        )
    except Exception as exc:
        ChatSequencer(db).abandon_pending_run(accepted.run.run_id, reason=str(exc))
        raise

    started_run_id = run.run_id
    _release_request_session(db)
    return sse_response(
        chat_run_executor.follow_run_as_sse(started_run_id, chat_id=chat_id),
    )


# ── Feedback ──────────────────────────────────────────────────────────────


@router.post(
    "/messages/{message_id}/ontology-revision/accept",
    summary="采用领域本体优化稿",
)
async def accept_ontology_revision(
    message_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Atomically replace one assistant message with its reviewed candidate answer."""
    chat_service = ChatService(db)
    target = chat_service.get_message_by_id(message_id)
    if not target or target.role != "assistant":
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)
    db_user_id = resolve_db_user_id(db, _authenticated_user_id(user))
    access = chat_service.get_session_with_access(target.chat_id, db_user_id)
    if access is None:
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)
    if access[1] not in ("admin", "edit"):
        raise HTTPException(status_code=403, detail="只读共享会话不可替换消息正文")
    updated = chat_service.accept_ontology_revision(message_id)
    if updated is None:
        raise HTTPException(status_code=409, detail="当前消息没有可采用的本体优化稿")
    return success_response(
        data={"message_id": message_id, "content": updated.content},
        message="已采用本体优化稿",
    )


class FeedbackRequest(BaseModel):
    rating: str
    comment: Optional[str] = None
    chat_id: Optional[str] = None


@router.post("/messages/{message_id}/feedback", summary="消息反馈")
async def submit_feedback(
    message_id: str,
    body: FeedbackRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """对某条助手消息提交点赞/点踩反馈（可附评论）。

    rating 仅接受 'like' / 'dislike'；同一用户对同一消息重复提交会覆盖原记录。
    """
    if body.rating not in ("like", "dislike"):
        raise HTTPException(status_code=400, detail="rating must be 'like' or 'dislike'")

    db_user_id = _authenticated_user_id(user)

    existing = (
        db.query(MessageFeedback)
        .filter(
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == db_user_id,
        )
        .first()
    )
    if existing:
        existing.rating = body.rating
        existing.comment = body.comment
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        record = existing
    else:
        record = MessageFeedback(
            message_id=message_id,
            chat_id=body.chat_id or "",
            user_id=db_user_id,
            rating=body.rating,
            comment=body.comment,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

    if body.rating == "dislike" and (body.comment or "").strip() and record.chat_id:
        try:
            from core.services.ontology_evolution_service import OntologyEvolutionService

            OntologyEvolutionService(db).ingest_user_correction(
                user_id=db_user_id,
                chat_id=record.chat_id,
                message_id=message_id,
                feedback_id=str(record.feedback_id),
                comment=(body.comment or "").strip(),
            )
        except Exception:  # noqa: BLE001 - feedback persistence must remain available
            logger.warning("ontology user-correction ingestion failed", exc_info=True)

    return {"ok": True, "feedback_id": record.feedback_id, "rating": record.rating}


# ---------------------------------------------------------------------------
# POST /v1/chats/{chat_id}/file-confirm  —— §13 MySpace write confirmation (out-of-band)
# ---------------------------------------------------------------------------


@router.get(
    "/{chat_id}/pending-user-questions",
    summary="查询会话中等待用户回答的问题",
)
async def get_pending_user_questions(
    chat_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authoritative pending queue for refresh/chat switching."""

    if ChatService(db).get_session(chat_id, user.user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import user_questions

    return success_response(
        data={"requests": user_questions.get_all_pending(chat_id)},
    )


@router.post(
    "/{chat_id}/user-questions/{request_id}/answer",
    summary="回答智能体主动提出的问题",
)
async def answer_user_question(
    chat_id: str,
    request_id: str,
    body: UserQuestionAnswerBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate the human answer and wake the exact suspended tool call."""

    if ChatService(db).get_session(chat_id, user.user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import user_questions

    result = user_questions.answer(
        chat_id,
        request_id,
        [item.model_dump(exclude_none=True) for item in body.answers],
    )
    if result.get("ok"):
        return success_response(data=result)
    if result.get("reason") == "stale":
        interrupted = _detect_chat_run_interrupted(db, chat_id)
        return success_response(
            data={
                **result,
                "stale": True,
                "chat_interrupted": interrupted,
                "message": (
                    "上次会话因服务端重启未完成，请重新发送您的消息"
                    if interrupted
                    else result.get("error", "该问题已失效")
                ),
            },
        )
    raise HTTPException(status_code=400, detail=result.get("error", "回答无效"))


@router.post(
    "/{chat_id}/user-questions/{request_id}/cancel",
    summary="取消智能体主动提出的问题",
)
async def cancel_user_question(
    chat_id: str,
    request_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel the pending request; the tool receives an explicit non-retry result."""

    if ChatService(db).get_session(chat_id, user.user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import user_questions

    result = user_questions.cancel(chat_id, request_id)
    if result.get("ok"):
        return success_response(data=result)
    interrupted = _detect_chat_run_interrupted(db, chat_id)
    return success_response(
        data={
            **result,
            "stale": True,
            "chat_interrupted": interrupted,
            "message": (
                "上次会话因服务端重启未完成，请重新发送您的消息"
                if interrupted
                else result.get("error", "该问题已失效")
            ),
        },
    )


class FileConfirmBody(BaseModel):
    confirm_id: str = Field(..., description="工具返回的 awaiting confirm_id")
    decision: str = Field(
        ..., description="allow | allow_session | deny | choice | skip（后两者为建站设计三选一）"
    )
    option_id: Optional[str] = Field(None, description="decision=choice 时必填：选中的设计方案 id")


@router.get("/{chat_id}/pending-confirm", summary="查询会话是否有待确认的我的空间写操作")
async def get_pending_confirm(
    chat_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """前端刷新/切回该会话时恢复确认条（§13）。无待确认项时 pendings=[]。"""
    session = ChatService(db).get_session(chat_id, user.user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import _myspace_confirm as _mc

    # pendings: all outstanding items; the frontend restores the whole confirmation queue from this
    # (one round of parallel tool calls can concurrently register N distinct pending confirmations).
    return success_response(
        data={
            "pendings": _mc.get_all_pending(chat_id),
        }
    )


@router.post("/{chat_id}/file-confirm", summary="确认/拒绝对我的空间的写操作")
async def file_confirm(
    chat_id: str,
    body: FileConfirmBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """用户带外批准/拒绝一次对「我的空间」的 Write/Edit/Delete/Move（§13）。

    模型**无法**自批——确认只能经此端点。批准后用户让模型重试同一操作，
    工具校验注册表通过即真正执行；拒绝则模型据工具反馈改走 /workspace。
    """
    # Ownership check: must be the caller's own session
    session = ChatService(db).get_session(chat_id, user.user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import _myspace_confirm as _mc

    res = _mc.set_decision(chat_id, body.confirm_id, body.decision, option_id=body.option_id)
    if not res.get("ok"):
        # An expired confirmation (timeout reclaim / process restart) is not the user's fault —
        # return 200 with a stale flag so the frontend silently dismisses the zombie confirmation
        # bar and shows a friendly hint, instead of popping a 400.
        if res.get("reason") == "stale":
            # Plan F mid-term fix: distinguish "ordinary timeout" from "interruption caused by a
            # server restart". The latter needs a clearer message for the user (not "confirmation
            # timed out" but "session was interrupted, please resend"); the frontend shows a more
            # prominent notice for it instead of silently dismissing the bar.
            chat_interrupted = _detect_chat_run_interrupted(db, chat_id)
            return success_response(
                data={
                    "ok": False,
                    "stale": True,
                    "chat_interrupted": chat_interrupted,
                    "message": (
                        "上次会话因服务端重启未完成，请重新发送您的消息"
                        if chat_interrupted
                        else res.get("error", "该确认已失效")
                    ),
                }
            )
        raise HTTPException(status_code=400, detail=res.get("error", "确认失败"))
    return success_response(data=res)


def _detect_chat_run_interrupted(db: Session, chat_id: str) -> bool:
    """Detect whether the chat's most recent run failed due to a server restart (within 30 minutes).

    Used to distinguish whether a ``/file-confirm`` stale result is an ordinary timeout reclaim,
    or a server restart that killed the whole agent task — in the latter case the user must resend
    the message, and a timeout-style hint would be very confusing.

    Returns False on any exception (stays compatible with the legacy stale path).
    """
    try:
        from datetime import timedelta, timezone

        from core.db.models import ChatRun

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        recent = (
            db.query(ChatRun.error_message)
            .filter(ChatRun.chat_id == chat_id)
            .filter(ChatRun.status == "failed")
            .filter(ChatRun.completed_at > cutoff)
            .order_by(ChatRun.completed_at.desc())
            .first()
        )
        if recent is None:
            return False
        err = (recent.error_message or "").lower()
        return "server restarted" in err or "server_restart" in err
    except (
        Exception
    ):  # noqa: BLE001 — on detection failure fall back to the old path; must not affect the main confirm flow
        return False
