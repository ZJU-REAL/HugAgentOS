"""Shared sessions operations; callers supply authenticated identity."""

import logging
from typing import Any, Dict, Optional

import core.chat.service as chat_services_ChatService_module

from core.auth.edition import can_delete_session

from core.infra.exceptions import ResourceNotFoundError

from core.infra.responses import created_response, paginated_response, success_response

from fastapi import HTTPException, status

from fastapi.responses import Response

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

def session_to_dict(s) -> dict:
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


def session_view_for_user(db: Session, s, user_id: str, level: str) -> dict:
    """Render a session for the current user, then let the edition extend it."""
    from core.chat.edition import extend_session_view

    return extend_session_view(db, s, user_id, level, session_to_dict(s))


def list_chats(
    page=1,
    page_size=20,
    sort="-updated_at",
    filter=None,
    exclude_automation=False,
    user=None,
    db=None,
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
    chat_service = chat_services_ChatService_module.ChatService(db)

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

    items = [session_to_dict(s) for s in sessions]

    return paginated_response(
        items=items,
        page=page,
        page_size=page_size,
        total_items=total,
        message="Chat sessions retrieved successfully",
    )


def create_chat(request, user=None, db=None):
    """
    Create a new chat session.

    The session is automatically associated with the current authenticated user.
    """
    chat_service = chat_services_ChatService_module.ChatService(db)
    if request.project_id:
        from core.projects.access import require_visible_project

        require_visible_project(db, user.user_id, request.project_id)

    session = chat_service.create_session(
        user_id=user.user_id,
        title=request.title,
        extra_data=request.metadata,
        project_id=request.project_id,
    )

    return created_response(
        data=session_to_dict(session), message="Chat session created successfully"
    )


def search_chats(q=..., scope="title", page=1, page_size=20, user=None, db=None):
    """
    Search chat sessions by title and optionally message content.

    - scope=title (default): search title only
    - scope=all: search both title and message content

    Returns sessions with match_type ("title" or "content") and matched_snippet for content matches.
    """
    chat_service = chat_services_ChatService_module.ChatService(db)

    results, total = chat_service.search_sessions(
        user_id=user.user_id,
        query=q,
        page=page,
        page_size=page_size,
        scope=scope,
    )

    items = []
    for r in results:
        item = session_to_dict(r["session"])
        item["match_type"] = r["match_type"]
        item["matched_snippet"] = r["matched_snippet"]
        items.append(item)

    return success_response(
        data={"items": items, "total": total}, message="Search completed successfully"
    )


def get_chat(chat_id, user=None, db=None):
    """获取当前用户有权读取的会话详情。"""
    chat_service = chat_services_ChatService_module.ChatService(db)

    pair = chat_service.get_session_with_access(chat_id, str(user.user_id))
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    session, level = pair

    return success_response(
        data=session_view_for_user(db, session, str(user.user_id), level),
        message="Chat session retrieved successfully",
    )


def update_chat(chat_id, request, user=None, db=None):
    """更新当前用户有权修改的会话元信息。"""
    chat_service = chat_services_ChatService_module.ChatService(db)
    user_id = str(user.user_id)

    pair = chat_service.get_session_with_access(chat_id, user_id)
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    session, level = pair
    # Serialize metadata merges and explicit renames. Refresh the identity-map
    # object under the lock so an older PATCH cannot clear a newer rename pin.
    from core.db.models import ChatSession

    session = db.query(ChatSession).filter(
        ChatSession.chat_id == chat_id,
        ChatSession.deleted_at.is_(None),
    ).populate_existing().with_for_update().one()

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

    from core.chat.edition import update_member_state

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
    if title_change is not None or (session.extra_data or {}).get("title_manually_set"):
        # Any explicit title PATCH is a user rename, including edits by shared
        # conversation editors. Auxiliary model results must not overwrite it.
        fields["extra_data"] = {**(fields.get("extra_data") or {}), "title_manually_set": True}

    if fields:
        chat_service.update_session_fields(chat_id, fields, actor_user_id=user_id)

    # Reload and render from the current user's perspective
    pair2 = chat_service.get_session_with_access(chat_id, user_id)
    if pair2 is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    s2, level2 = pair2
    return success_response(
        data=session_view_for_user(db, s2, user_id, level2),
        message="Chat session updated successfully",
    )


def delete_chat(chat_id, user=None, db=None):
    """软删当前用户有权管理的会话。"""
    chat_service = chat_services_ChatService_module.ChatService(db)
    user_id = str(user.user_id)

    # First check whether the session is visible to the current user — return 404 if not, to avoid leaking its existence
    pair = chat_service.get_session_with_access(chat_id, user_id)
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    session, _level = pair

    if not can_delete_session(db, user_id, session):
        raise HTTPException(status_code=403, detail="共享会话仅创建者或项目管理员可删")

    from core.llm.mcp.chat_sessions import invalidate_chat
    invalidate_chat(chat_id)
    chat_service.delete_session_force(chat_id, actor_user_id=user_id)
    # The upload snapshots go with the chat; My Space keeps its own copy. Best effort: the
    # chat is already deleted, a failed cleanup only leaves storage behind.
    try:
        from core.files.artifacts.chat_snapshots import release_chat_snapshots
        from core.files.storage import get_storage
        release_chat_snapshots(db, chat_id, get_storage())
    except Exception:
        db.rollback()
        logger.warning("Failed to release upload snapshots of chat %s", chat_id, exc_info=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
