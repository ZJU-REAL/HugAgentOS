"""My Space — user resource management API

GET    /v1/artifacts            user file/image list
GET    /v1/artifacts/favorites  favorited conversation list
DELETE /v1/artifacts/{id}       soft-delete a resource
"""

import asyncio
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.auth.backend import UserContext, get_current_user
from core.content.kb_processing import vectorise_document_background
from core.db.engine import SessionLocal, get_db
from core.db.models import Artifact, ChatMessage, ChatSession, KBDocument, KBSpace, UserShadow
from core.db.paging import DEFAULT_PAGE_SIZE
from core.db.repository import ArtifactRepository
from core.infra.responses import error_response, success_response
from core.services import KBService
from core.services.artifact_edition import (
    artifact_list_scope,
    artifact_scope_fields,
    can_access_artifact,
    extend_artifact_item,
)
from core.storage import get_storage
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

# Users whose historical data has already been backfilled. The authoritative marker
# is durable (``users_shadow.metadata.artifacts_backfilled_at``) — a process-lifetime
# set alone replays the whole scan after every restart, which froze the backend for
# minutes on the first 「我的空间」 open. The set below only caches that marker so the
# steady-state path costs no query. ``_backfill_lock`` serialises the claim so the
# scan body runs at most once per user per process; concurrent polls skip it (the
# listing still works off whatever is already committed).
_BACKFILL_MARKER = "artifacts_backfilled_at"
_BACKFILL_BATCH = 500
_backfilled_users: set = set()
_backfilling_users: set = set()
_backfill_lock = threading.Lock()
# Strong refs to the detached scans; asyncio only holds weak ones.
_backfill_tasks: set = set()


def _backfill_already_done(db: Session, user_id: str) -> bool:
    row = db.query(UserShadow.extra_data).filter(UserShadow.user_id == user_id).first()
    return bool(row and (row[0] or {}).get(_BACKFILL_MARKER))


def _mark_backfill_done(db: Session, user_id: str) -> None:
    from core.services.user_service import UserService

    UserService(db).update_user_metadata(
        user_id, {_BACKFILL_MARKER: datetime.now(timezone.utc).isoformat()}
    )


def _run_backfill_once(user_id: str) -> None:
    """Run the historical scan at most once per user, ever. Blocking — off the loop.

    Owns its own Session: it is handed to a worker thread and must not share the
    request's one. Only a clean run stamps the durable marker, so a failure retries
    on the next open instead of silently dropping the user's old files.
    """
    with _backfill_lock:
        if user_id in _backfilled_users or user_id in _backfilling_users:
            return
        _backfilling_users.add(user_id)
    db = SessionLocal()
    try:
        if not _backfill_already_done(db, user_id):
            _backfill_artifacts_from_messages(user_id, db)
            _mark_backfill_done(db, user_id)
        with _backfill_lock:
            _backfilled_users.add(user_id)
    except Exception:  # noqa: BLE001 — a failed backfill must not break the listing
        logger.warning("backfill_artifacts failed for user %s", user_id, exc_info=True)
        db.rollback()
        # Stand down for this process. The durable marker is untouched, so the scan
        # retries after a restart — without it, every panel open would re-run a
        # full-history scan for an account whose backfill keeps failing.
        with _backfill_lock:
            _backfilled_users.add(user_id)
    finally:
        db.close()
        with _backfill_lock:
            _backfilling_users.discard(user_id)


# 列「我的空间」之前先催一下登记器：沙箱写文件是随时发生的，去抖窗口里刚落盘的那几个
# 不该等到下次刷新才出现。催的是同一个登记器（core.space_sync.personal），不是另开一条对账
# 路径 —— 登记在哪儿发生、按什么判据发生，都还是它说了算。


class AddArtifactToKBRequest(BaseModel):
    kb_id: str


# ── Shared artifact-ref helpers ───────────────────────────────────────────
# Moved to core.content.artifact_refs so lower layers can use them without
# importing this API route module. Re-exported here for existing call sites.
from core.content.artifact_refs import extract_file_refs, infer_artifact_type  # noqa: E402


def sanitize_chat_preview(content: Optional[str], max_len: int = 200) -> str:
    """Normalize chat preview text for list cards.

    Favorite chat previews should stay single-paragraph and avoid control
    characters or excessive whitespace from raw message content.
    """
    if not content:
        return ""

    text = str(content)
    # assistant \u6d88\u606f\u7684 content \u5728\u5e93\u91cc\u662f\u300c\u601d\u80031</think>\u601d\u80032</think>\u6b63\u6587\u300d\u7684\u539f\u59cb\u4e32\uff0c
    # \u5361\u7247\u6458\u8981\u53ea\u8981\u6700\u7ec8\u6b63\u6587\u2014\u2014\u53d6\u6700\u540e\u4e00\u4e2a </think> \u4e4b\u540e\u7684\u90e8\u5206\uff0c\u5e76\u5265\u6389\u53ef\u80fd\u6b8b\u7559\u7684
    # \u672a\u95ed\u5408 <think> \u8d77\u59cb\u6bb5\uff08\u622a\u65ad\u573a\u666f\uff09\uff0c\u907f\u514d\u6536\u85cf\u5361\u7247\u5c55\u793a\u601d\u8003\u8fc7\u7a0b/\u6807\u7b7e\uff08\u95ee\u98988\uff09\u3002
    if "</think>" in text:
        tail = text.rsplit("</think>", 1)[-1]
        # \u5168\u662f\u601d\u8003\u6ca1\u6709\u6b63\u6587\u7684\u6781\u7aef\u60c5\u51b5\uff1a\u9000\u56de\u53bb\u6807\u7b7e\u540e\u7684\u539f\u6587\uff0c\u522b\u8ba9\u6458\u8981\u53d8\u6210\u7a7a\u767d
        text = tail if tail.strip() else re.sub(r"</?think>", " ", text)
    if "<think>" in text:
        head = text.split("<think>", 1)[0]
        text = head if head.strip() else re.sub(r"</?think>", " ", text)
    text = text.replace("\ufeff", "").replace("\u200b", "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[:max_len].rstrip() + "…"
    return text


# ── Backfill (runs once per user, ever — see the durable marker above) ────


from core.services.artifact_backfill import _backfill_artifacts_from_messages


def _collect_artifact_kb_usage(
    db: Session, user_id: str, artifact_ids: List[str]
) -> Dict[str, List[Dict[str, str]]]:
    """Collect private KB memberships for a batch of artifact IDs."""
    if not artifact_ids:
        return {}

    usage: Dict[str, List[Dict[str, str]]] = {artifact_id: [] for artifact_id in artifact_ids}
    rows = (
        db.query(KBDocument, KBSpace)
        .join(KBSpace, KBDocument.kb_id == KBSpace.kb_id)
        .filter(
            KBSpace.user_id == user_id,
            KBSpace.deleted_at.is_(None),
            KBDocument.deleted_at.is_(None),
        )
        .all()
    )

    artifact_id_set = set(artifact_ids)
    for document, space in rows:
        meta = document.extra_data if isinstance(document.extra_data, dict) else {}
        source_artifact_id = meta.get("source_artifact_id")
        if not source_artifact_id or source_artifact_id not in artifact_id_set:
            continue
        usage.setdefault(source_artifact_id, []).append(
            {
                "kb_id": space.kb_id,
                "name": space.name,
            }
        )

    return usage


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("/favorites", summary="收藏会话列表")
def list_favorite_chats(
    keyword: Optional[str] = Query(None, description="搜索关键字"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取用户收藏的会话列表（含最后消息预览，单次查询）。"""
    uid = str(user.user_id)

    # Build base query with keyword filter pushed to SQL
    q = db.query(ChatSession).filter(
        ChatSession.user_id == uid,
        ChatSession.deleted_at.is_(None),
        ChatSession.favorite == True,  # noqa: E712
    )
    if keyword:
        q = q.filter(ChatSession.title.ilike(f"%{keyword}%"))

    total = q.count()
    sessions = (
        q.order_by(desc(ChatSession.updated_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # Batch-fetch last message preview for all sessions in one query
    chat_ids = [s.chat_id for s in sessions]
    previews: Dict[str, str] = {}
    if chat_ids:
        # Window function: row_number per chat_id ordered by durable chat order.
        rn = (
            func.row_number()
            .over(
                partition_by=ChatMessage.chat_id,
                order_by=desc(ChatMessage.chat_seq),
            )
            .label("rn")
        )
        subq = (
            db.query(ChatMessage.chat_id, ChatMessage.content, rn)
            .filter(
                ChatMessage.chat_id.in_(chat_ids),
                ChatMessage.role.in_(["user", "assistant"]),
            )
            .subquery()
        )
        rows = db.query(subq.c.chat_id, subq.c.content).filter(subq.c.rn == 1).all()
        for cid, content in rows:
            previews[cid] = sanitize_chat_preview(content, max_len=200)

    items = []
    for s in sessions:
        items.append(
            {
                "id": s.chat_id,
                "type": "favorite",
                "name": s.title or "对话",
                "source_chat_id": s.chat_id,
                "source_chat_title": s.title,
                "content_preview": previews.get(s.chat_id, ""),
                "created_at": (
                    (s.updated_at or s.created_at).isoformat()
                    if (s.updated_at or s.created_at)
                    else None
                ),
            }
        )

    return success_response(
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < total,
        }
    )


@router.get("", summary="用户文件/图片列表")
async def list_user_artifacts(
    type: Optional[str] = Query(None, description="document | image"),
    source_kind: Optional[str] = Query(None, description="user_upload | ai_generated"),
    keyword: Optional[str] = Query(None, description="文件名搜索"),
    scope: str = Depends(artifact_list_scope),
    folder_id: Optional[str] = Query(
        None,
        description="仅 personal scope 生效：__root__=个人根目录，<id>=该个人文件夹直接子文件，省略=全部个人文件（向后兼容）",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, description="每页条数，无上限"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取当前用户有权查看的文件与图片。"""
    uid = str(user.user_id)

    # One-time backfill for historical data. It scans every message the account ever
    # wrote, so it never runs inline — doing that on the event loop froze the whole
    # backend for minutes. It also must not go through ``BackgroundTasks``: those run
    # on the shared request threadpool, which every endpoint needs via ``get_db``
    # (see the postmortem at the top of core/kb/index_queue.py). Detached to the
    # default executor instead, like the mirror reconcile below. The rows it adds are
    # historical, so the next listing picks them up.
    if uid not in _backfilled_users:
        task = asyncio.create_task(asyncio.to_thread(_run_backfill_once, uid))
        _backfill_tasks.add(task)
        task.add_done_callback(_backfill_tasks.discard)

    if scope != "all":
        from core.space_sync.personal_registry import flush_user

        await flush_user(uid, metadata_only=True)

    repo = ArtifactRepository(db)
    mime_prefix = None
    if type == "image":
        mime_prefix = "image/"
    elif type == "document":
        mime_prefix = "document"

    normalized_source_kind = source_kind if source_kind in ("user_upload", "ai_generated") else None
    personal_only = scope != "all"

    rows, total = repo.list_by_user_with_chat(
        user_id=uid,
        mime_prefix=mime_prefix,
        keyword=keyword,
        source_kind=normalized_source_kind,
        page=page,
        page_size=page_size,
        personal_only=personal_only,
        folder_id=folder_id if personal_only else None,
    )

    artifact_ids = [row["artifact"].artifact_id for row in rows]
    artifact_kb_usage = _collect_artifact_kb_usage(db, uid, artifact_ids)

    items = []
    for row in rows:
        artifact = row["artifact"]
        is_image = artifact.mime_type and artifact.mime_type.startswith("image/")
        linked_kbs = artifact_kb_usage.get(artifact.artifact_id, [])
        extra_data = artifact.extra_data if isinstance(artifact.extra_data, dict) else {}
        source_kind = "user_upload" if extra_data.get("source") == "user_upload" else "ai_generated"
        item = {
            "id": artifact.artifact_id,
            "type": "image" if is_image else "document",
            "name": artifact.filename or artifact.title,
            "mime_type": artifact.mime_type,
            "file_id": artifact.artifact_id,
            "size": artifact.size_bytes,
            "source_kind": source_kind,
            "knowledge_base_count": len(linked_kbs),
            "knowledge_bases": linked_kbs,
            "source_chat_id": artifact.chat_id,
            "source_chat_title": row["chat_title"] or "对话",
            "user_folder_id": artifact.user_folder_id,
            "created_at": (
                (artifact.updated_at or artifact.created_at).isoformat()
                if (artifact.updated_at or artifact.created_at)
                else None
            ),
        }
        items.append(extend_artifact_item(artifact, item))

    return success_response(
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < total,
        }
    )


@router.post("/{artifact_id}/knowledge-base", summary="资源加入知识库")
def add_artifact_to_knowledge_base(
    artifact_id: str,
    payload: AddArtifactToKBRequest,
    background_tasks: BackgroundTasks,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """将有权访问的资源加入目标知识库；已存在时直接返回。"""
    uid = str(user.user_id)
    kb_service = KBService(db)

    try:
        document = kb_service.add_artifact_to_space(
            artifact_id=artifact_id,
            user_id=uid,
            kb_id=payload.kb_id,
        )
    except ValueError as exc:
        return error_response(message=str(exc), code=404, status_code=404)
    except PermissionError as exc:
        return error_response(message=str(exc), code=403, status_code=403)

    if document.get("already_exists"):
        return success_response(data=document, message="该文件已在目标知识库中")

    try:
        artifact = ArtifactRepository(db).get_by_id(artifact_id)
        if artifact is None:
            return error_response(message="资源不存在或无权限", code=404, status_code=404)
        if not can_access_artifact(db, uid, artifact):
            return error_response(message="资源不存在或无权限", code=404, status_code=404)
        file_bytes = get_storage().download_bytes(artifact.storage_key)
        background_tasks.add_task(
            vectorise_document_background,
            document_id=document["document_id"],
            kb_id=payload.kb_id,
            user_id=uid,
            title=document["title"],
            file_bytes=file_bytes,
            mime_type=artifact.mime_type or "application/octet-stream",
            chunk_method=document["chunk_method"],
            db_url=os.getenv("DATABASE_URL", ""),
            indexing_config=document.get("indexing_config"),
        )
    except Exception:
        logger.warning("failed to queue indexing for artifact %s", artifact_id, exc_info=True)

    return success_response(data=document, message="文件已加入知识库，正在索引")


@router.delete("/{artifact_id}", summary="删除资源")
def delete_artifact(
    artifact_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """软删除资源。"""
    repo = ArtifactRepository(db)
    uid = str(user.user_id)
    deleted = repo.soft_delete_owned(artifact_id, uid)
    if not deleted:
        return error_response(message="资源不存在或无权限", code=404, status_code=404)
    return success_response(message="删除成功")
