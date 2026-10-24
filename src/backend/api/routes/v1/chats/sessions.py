"""Session lifecycle, discovery, search and reference selection endpoints."""

from typing import Optional

import core.chat.operations.schemas as chat_models
import core.chat.operations.session_context as chat_session_context
import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.db.engine as db_engine
from core.auth.backend import UserContext
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

router = APIRouter()


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
    from core.chat.operations.sessions import list_chats as operation

    return operation(
        page=page,
        page_size=page_size,
        sort=sort,
        filter=filter,
        exclude_automation=exclude_automation,
        user=user,
        db=db,
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
    from core.chat.operations.sessions import create_chat as operation

    return operation(request=request, user=user, db=db)


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
    from core.chat.operations.sessions import search_chats as operation

    return operation(q=q, scope=scope, page=page, page_size=page_size, user=user, db=db)


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
    from core.chat.references import list_referencable_sessions, session_brief

    db_user_id = chat_context.resolve_db_user_id(
        db, chat_session_context.authenticated_user_id(user)
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
    from core.chat.operations.sessions import get_chat as operation

    return operation(chat_id=chat_id, user=user, db=db)


@router.patch("/{chat_id}", summary="更新会话")
def update_chat(
    chat_id: str,
    request: chat_models.UpdateChatRequest,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """更新当前用户有权修改的会话元信息。"""
    from core.chat.operations.sessions import update_chat as operation

    return operation(chat_id=chat_id, request=request, user=user, db=db)


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除会话")
def delete_chat(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """软删当前用户有权管理的会话。"""
    from core.chat.operations.sessions import delete_chat as operation

    return operation(chat_id=chat_id, user=user, db=db)
