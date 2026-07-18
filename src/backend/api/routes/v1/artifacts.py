"""My Space — user resource management API

GET    /v1/artifacts            user file/image list
GET    /v1/artifacts/favorites  favorited conversation list
DELETE /v1/artifacts/{id}       soft-delete a resource
"""

import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.auth.backend import get_current_user, UserContext
from core.content.kb_processing import vectorise_document_background
from core.db.engine import get_db
from core.db.models import Artifact, ChatMessage, ChatSession, KBDocument, KBSpace
from core.db.repository import ArtifactRepository
from core.infra.responses import success_response, error_response
from core.services import KBService
from core.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

# Users whose historical data has already been backfilled (process-lifetime cache).
# A sync FastAPI endpoint runs in the threadpool, so concurrent ``/v1/artifacts``
# polls genuinely race. ``_backfill_lock`` serialises the claim so the backfill
# body runs at most once per user per process; concurrent requests skip it (the
# listing still works off whatever is already committed).
_backfilled_users: set = set()
_backfilling_users: set = set()
_backfill_lock = threading.Lock()


class AddArtifactToKBRequest(BaseModel):
    kb_id: str


# ── Shared artifact-ref helpers ───────────────────────────────────────────
# Moved to core.content.artifact_refs so lower layers can use them without
# importing this API route module. Re-exported here for existing call sites.
from core.content.artifact_refs import (  # noqa: E402
    extract_file_ref,
    extract_file_refs,
    infer_artifact_type,
    resolve_artifact_storage_key,
)


def sanitize_chat_preview(content: Optional[str], max_len: int = 200) -> str:
    """Normalize chat preview text for list cards.

    Favorite chat previews should stay single-paragraph and avoid control
    characters or excessive whitespace from raw message content.
    """
    if not content:
        return ""

    text = str(content)
    text = text.replace("\ufeff", "").replace("\u200b", "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[:max_len].rstrip() + "…"
    return text


# ── Backfill (runs once per user per process) ─────────────────────────────


def _backfill_artifacts_from_messages(user_id: str, db: Session) -> int:
    """Scan historical messages for file references not yet in the Artifact table.

    Source priority depends on whether the message was written under the
    strict ``pin_to_workspace`` regime:

      • Strict-mode message (``extra_data.workspace_files`` is present —
        even as ``[]``): trust ``extra_data.artifacts`` only. Do NOT
        scrape ``tool_calls[].result``, since those carry every transient
        docx the agent emitted but did not pin.
      • Legacy message (no ``workspace_files`` field): fall back to
        scraping ``tool_calls[].result`` for AI-generated files so old
        chats don't lose their files. Plus ``extra_data.artifacts``.

    User attachments (``extra_data.attachments``) are always scraped —
    those represent files the user explicitly uploaded.
    """
    # Dedup against the GLOBAL artifact_id space, not this user's rows:
    # ``Artifact.artifact_id`` is a single-column global primary key, so a
    # content-hash file id already owned by another user/chat would otherwise
    # slip past a user-filtered set and blow up the whole INSERT batch.
    existing_ids = set(
        row[0] for row in db.query(Artifact.artifact_id).all()
    )

    # Scan both assistant messages (tool_calls) and user messages (attachments).
    # Also carry the chat session's project_id — under a team project, backfilled
    # rows must land on team_id+team_folder_id; otherwise a fallback backfill
    # triggered by a single list request would pour a team chat's historical
    # files into the personal MySpace root.
    rows = (
        db.query(
            ChatMessage.chat_id,
            ChatMessage.role,
            ChatMessage.tool_calls,
            ChatMessage.extra_data,
            ChatSession.project_id,
        )
        .join(ChatSession, ChatMessage.chat_id == ChatSession.chat_id)
        .filter(
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
            ChatMessage.role.in_(["assistant", "user"]),
        )
        .all()
    )

    # Cache project_id → (team_id, team_folder_id, user_folder_id) to reduce
    # repeated lookups.
    # Any missing item → treated as non-project; on write all NULL, landing in
    # the MySpace root (consistent with old behavior).
    from core.services.project_scope import project_scope_from_chat_id  # local: avoid top-level cycle
    _project_columns_cache: Dict[str, tuple] = {}

    def _columns_for_chat(chat_id: str) -> tuple:
        # Reverse-lookup via project_scope_from_chat_id; personal hit →
        # user_folder_id; team hit → team_id + team_folder_id; otherwise all None.
        scope = project_scope_from_chat_id(db, chat_id)
        if scope is None:
            return (None, None, None)
        if scope.is_team:
            return (None, scope.team_id, scope.root_folder_id)
        return (scope.root_folder_id, None, None)

    created = 0
    for chat_id, role, tool_calls_col, extra_data, _project_id in rows:
        file_refs: List[Dict[str, Any]] = []
        is_strict_message = (
            isinstance(extra_data, dict)
            and extra_data.get("workspace_files") is not None
        )

        # Source 1: tool_calls[].result (legacy assistant messages only)
        if role == "assistant" and not is_strict_message:
            for tc in (tool_calls_col or []):
                file_refs.extend(extract_file_refs(tc.get("result")))

        if isinstance(extra_data, dict):
            # Source 2: extra_data.artifacts — pinned-only under strict mode
            for art in (extra_data.get("artifacts") or []):
                file_refs.extend(extract_file_refs(art))

            # Source 3: extra_data.attachments (user uploads — always)
            for att in (extra_data.get("attachments") or []):
                file_refs.extend(extract_file_refs(att))

        if not file_refs:
            continue

        # Resolve ownership columns per chat (personal/team/no-project); multiple
        # refs within the same chat reuse them.
        if _project_id and _project_id in _project_columns_cache:
            _bf_user_folder, _bf_team_id, _bf_team_folder = _project_columns_cache[_project_id]
        else:
            _bf_user_folder, _bf_team_id, _bf_team_folder = _columns_for_chat(chat_id)
            if _project_id:
                _project_columns_cache[_project_id] = (
                    _bf_user_folder, _bf_team_id, _bf_team_folder,
                )

        for ref in file_refs:
            fid = ref["file_id"]
            if fid in existing_ids:
                continue
            # Per-row SAVEPOINT: a residual collision (cross-process race, or a
            # duplicate id we couldn't see) rolls back ONLY this row, never the
            # whole batch. Plain ``db.add`` + a single trailing commit would let
            # one bad row abort the entire transaction (the old behaviour).
            existing_ids.add(fid)  # claim before insert so dup refs in-batch skip
            try:
                with db.begin_nested():
                    db.add(Artifact(
                        artifact_id=fid,
                        chat_id=chat_id,
                        user_id=user_id,
                        user_folder_id=_bf_user_folder,
                        team_id=_bf_team_id,
                        team_folder_id=_bf_team_folder,
                        type=infer_artifact_type(ref["mime_type"]),
                        title=ref["name"],
                        filename=ref["name"],
                        size_bytes=max(ref.get("size", 0) or 0, 1),
                        mime_type=ref["mime_type"],
                        storage_key=ref.get("storage_key") or f"artifacts/{fid}",
                        storage_url=ref.get("url", ""),
                        extra_data={"source": "backfill"},
                    ))
                created += 1
            except IntegrityError:
                logger.debug("backfill skip dup %s", fid, exc_info=True)
            except Exception:
                logger.debug("backfill skip %s", fid, exc_info=True)

    if created:
        try:
            db.commit()
            logger.info("backfill_artifacts: created %d for user %s", created, user_id)
        except Exception:
            logger.warning("backfill_artifacts commit failed", exc_info=True)
            db.rollback()
            created = 0
    return created


def _collect_artifact_kb_usage(db: Session, user_id: str, artifact_ids: List[str]) -> Dict[str, List[Dict[str, str]]]:
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
        usage.setdefault(source_artifact_id, []).append({
            "kb_id": space.kb_id,
            "name": space.name,
        })

    return usage


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("/favorites", summary="收藏会话列表")
async def list_favorite_chats(
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
    sessions = q.order_by(desc(ChatSession.updated_at)).offset(
        (page - 1) * page_size
    ).limit(page_size).all()

    # Batch-fetch last message preview for all sessions in one query
    chat_ids = [s.chat_id for s in sessions]
    previews: Dict[str, str] = {}
    if chat_ids:
        # Window function: row_number per chat_id ordered by created_at desc
        rn = func.row_number().over(
            partition_by=ChatMessage.chat_id,
            order_by=desc(ChatMessage.created_at),
        ).label("rn")
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
        items.append({
            "id": s.chat_id,
            "type": "favorite",
            "name": s.title or "对话",
            "source_chat_id": s.chat_id,
            "source_chat_title": s.title,
            "content_preview": previews.get(s.chat_id, ""),
            "created_at": (s.updated_at or s.created_at).isoformat() if (s.updated_at or s.created_at) else None,
        })

    return success_response(data={
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
    })


@router.get("", summary="用户文件/图片列表")
async def list_user_artifacts(
    type: Optional[str] = Query(None, description="document | image"),
    source_kind: Optional[str] = Query(None, description="user_upload | ai_generated"),
    keyword: Optional[str] = Query(None, description="文件名搜索"),
    scope: Optional[str] = Query("personal", description="personal（默认，仅个人文件）| all（含团队）"),
    folder_id: Optional[str] = Query(
        None,
        description="仅 personal scope 生效：__root__=个人根目录，<id>=该个人文件夹直接子文件，省略=全部个人文件（向后兼容）",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取用户文件/图片列表（从 Artifact 表）。默认仅返回个人（非团队归属）文件。"""
    uid = str(user.user_id)

    # One-time backfill for historical data. Claim under a lock BEFORE running
    # the (slow) scan so concurrent polls don't all re-enter and collide; losers
    # skip and serve whatever's already committed (next poll sees the backfill).
    should_backfill = False
    with _backfill_lock:
        if uid not in _backfilled_users and uid not in _backfilling_users:
            _backfilling_users.add(uid)
            should_backfill = True
    if should_backfill:
        try:
            _backfill_artifacts_from_messages(uid, db)
            with _backfill_lock:
                _backfilled_users.add(uid)
        finally:
            with _backfill_lock:
                _backfilling_users.discard(uid)

    repo = ArtifactRepository(db)
    mime_prefix = None
    if type == "image":
        mime_prefix = "image/"
    elif type == "document":
        mime_prefix = "document"

    normalized_source_kind = source_kind if source_kind in ("user_upload", "ai_generated") else None
    personal_only = scope != "all"

    rows, total = repo.list_by_user_with_chat(
        user_id=uid, mime_prefix=mime_prefix, keyword=keyword,
        source_kind=normalized_source_kind,
        page=page, page_size=page_size,
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
        items.append({
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
            "team_id": artifact.team_id,
            "team_folder_id": artifact.team_folder_id,
            "user_folder_id": artifact.user_folder_id,
            "created_at": (artifact.updated_at or artifact.created_at).isoformat() if (artifact.updated_at or artifact.created_at) else None,
        })

    return success_response(data={
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
    })


@router.post("/{artifact_id}/knowledge-base", summary="资源加入知识库")
async def add_artifact_to_knowledge_base(
    artifact_id: str,
    payload: AddArtifactToKBRequest,
    background_tasks: BackgroundTasks,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """将指定资源加入目标知识库（kb_id），校验归属/团队权限后后台异步向量化索引；文件已存在时直接返回。"""
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
        from core.auth.permissions_iface import has_permission, resolve_artifact_access

        artifact = ArtifactRepository(db).get_by_id(artifact_id)
        if artifact is None:
            return error_response(message="资源不存在或无权限", code=404, status_code=404)
        # Single point that combines owner ∪ team permissions (owner always
        # allowed; team members with view+ can reference)
        perm = resolve_artifact_access(db, uid, artifact.user_id, artifact.team_id)
        if not has_permission(perm, "view"):
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
async def delete_artifact(
    artifact_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """软删除资源。"""
    repo = ArtifactRepository(db)
    uid = str(user.user_id)
    deleted = repo.soft_delete(artifact_id, uid)
    if not deleted:
        return error_response(message="资源不存在或无权限", code=404, status_code=404)
    return success_response(message="删除成功")
