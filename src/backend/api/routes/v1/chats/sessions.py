"""Session lifecycle, discovery, search and reference selection endpoints."""

from typing import Any, Dict, Optional

import api.routes.v1.chats.models as chat_models
import api.routes.v1.chats.session_context as chat_session_context
import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.db.engine as db_engine
import core.services as chat_services
from core.auth.backend import UserContext
from core.auth.permissions_iface import can_delete_session
from core.infra.exceptions import ResourceNotFoundError
from core.infra.responses import created_response, paginated_response, success_response
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

router = APIRouter()


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


@router.get("", summary="获取会话列表")
def list_chats(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    sort: str = Query("-updated_at", description="Sort field"),
    filter: Optional[str] = Query(None, description="Filter conditions"),
    exclude_automation: bool = Query(False, description="Exclude automation-generated chats"),
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
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
    chat_service = chat_services.ChatService(db)

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
def create_chat(
    request: chat_models.CreateChatRequest,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """
    Create a new chat session.

    The session is automatically associated with the current authenticated user.
    """
    chat_service = chat_services.ChatService(db)

    session = chat_service.create_session(
        user_id=user.user_id, title=request.title, extra_data=request.metadata
    )

    return created_response(
        data=_session_to_dict(session), message="Chat session created successfully"
    )


@router.get("/search", summary="搜索会话")
def search_chats(
    q: str = Query(..., description="Search keyword"),
    scope: str = Query(
        "title", description="Search scope: 'title' or 'all' (title + message content)"
    ),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """
    Search chat sessions by title and optionally message content.

    - scope=title (default): search title only
    - scope=all: search both title and message content

    Returns sessions with match_type ("title" or "content") and matched_snippet for content matches.
    """
    chat_service = chat_services.ChatService(db)

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


@router.get("/referencable", summary="获取可引用的历史会话")
def list_referencable_chats(
    q: str = Query("", description="可选关键词，按标题和消息正文筛选"),
    project_id: Optional[str] = Query(None, description="限定在该项目内检索"),
    exclude_chat_id: Optional[str] = Query(None, description="排除当前正在进行的会话"),
    limit: int = Query(20, ge=1, le=50, description="返回条数"),
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """输入框里挑选要引用的历史会话（斜杠命令面板与拖拽落点共用）。

    在项目里只给同项目的会话——项目是用户自己划的话题边界。只返回标题级信息，正文由
    智能体在回答时按需调 ``read_chat`` 读取。
    """
    from core.services.chat_reference_service import list_referencable_sessions, session_brief

    db_user_id = chat_context.resolve_db_user_id(
        db, chat_session_context._authenticated_user_id(user)
    )
    sessions = list_referencable_sessions(
        db,
        db_user_id,
        project_id=project_id,
        query=q,
        limit=limit,
        exclude_chat_id=exclude_chat_id,
    )
    return success_response(
        data={"items": [session_brief(s) for s in sessions]},
        message="Referencable chats retrieved successfully",
    )


@router.get("/{chat_id}", summary="获取会话详情")
def get_chat(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """获取当前用户有权读取的会话详情。"""
    chat_service = chat_services.ChatService(db)

    pair = chat_service.get_session_with_access(chat_id, str(user.user_id))
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    session, level = pair

    return success_response(
        data=_session_view_for_user(db, session, str(user.user_id), level),
        message="Chat session retrieved successfully",
    )


@router.patch("/{chat_id}", summary="更新会话")
def update_chat(
    chat_id: str,
    request: chat_models.UpdateChatRequest,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """更新当前用户有权修改的会话元信息。"""
    chat_service = chat_services.ChatService(db)
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
def delete_chat(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """软删当前用户有权管理的会话。"""
    chat_service = chat_services.ChatService(db)
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
