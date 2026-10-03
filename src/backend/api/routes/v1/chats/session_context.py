"""Session, identity, model and history preparation for chat execution."""

from typing import Any, Dict, List, Optional

import core.services as chat_services
from api.schemas import ChatRequest
from core.auth.backend import UserContext
from core.chat.context import generate_smart_title
from core.infra.logging import get_logger
from core.services.model_config import ModelConfigService
from core.services.user_model_selection import (
    UserModelSelectionError,
    resolve_effective_chat_model_name,
    resolve_user_model_provider_id,
)
from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = get_logger(__name__)


def _clean_id_list(raw: Optional[list]) -> List[str]:
    """Normalize a list of capability IDs: strip whitespace, remove empties."""
    if not isinstance(raw, list):
        return []
    return [str(s).strip() for s in raw if str(s).strip()]


def _authenticated_user_id(user: Optional[UserContext]) -> Optional[str]:
    if isinstance(user, UserContext):
        return user.user_id
    return None


def _ensure_main_model_configured() -> None:
    """Fail fast with a user-facing error when the main chat model is missing."""
    resolved = ModelConfigService.get_instance().resolve("main_agent")
    if resolved is not None:
        return
    raise HTTPException(
        status_code=503,
        detail="当前未配置主对话模型，请先在管理后台配置模型供应商并绑定 main_agent 角色。",
    )


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


def _ensure_chat_session(
    chat_service: chat_services.ChatService,
    chat_id: str,
    user_id: str,
    first_message: str,
    agent_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    plan_chat: bool = False,
    batch_chat: bool = False,
    workflow_chat: bool = False,
    site_chat: bool = False,
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
    if site_chat:
        extra_data["site_chat"] = True
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
    if site_chat and not existing_meta.get("site_chat"):
        merged["site_chat"] = True
        dirty = True
    if dirty:
        chat_service.update_session(chat_id, user_id, {"extra_data": merged})
    return session


def _load_session_messages(
    chat_service: chat_services.ChatService, chat_id: str, user_id: str
) -> List[Dict[str, Any]]:
    # Checkpoint-aware history loading: when a checkpoint exists, fetch only the tail messages from the DB (no more load-everything-then-discard).
    # Cross-turn tool results are not truncated. See core/services/compaction_service.py.
    from core.services.compaction_service import load_session_history

    messages = load_session_history(chat_service, chat_id, user_id)
    if messages is None:
        raise HTTPException(status_code=404, detail=f"Session {chat_id} not found")
    return messages


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
