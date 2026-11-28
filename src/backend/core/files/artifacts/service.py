"""Artifact management business logic."""

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional

from core.files.artifacts.refs import infer_artifact_type, require_artifact_storage_key
from core.db.models import Artifact as ArtifactModel
from core.db.repository import ArtifactRepository
from core.files.edition import artifact_name_target, artifact_scope_fields
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from core.projects.edition import ProjectScope

logger = logging.getLogger(__name__)


def artifact_upload_key(user_id: str, artifact_id: str, filename: str) -> str:
    """Canonical storage location, also used for safe compensation before SQL commit."""
    env = os.getenv("ENVIRONMENT", "dev")
    return f"{env}/{user_id}/user_uploads/{artifact_id}/{filename}"


def store_bytes_as_artifact(
    db: Session,
    *,
    user_id: str,
    content: bytes,
    filename: str,
    mime_type: Optional[str] = None,
    chat_id: Optional[str] = None,
    user_folder_id: Optional[str] = None,
    source: str = "user_upload",
    parsed_text: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    artifact_id: Optional[str] = None,
    commit: bool = True,
) -> ArtifactModel:
    """Persist bytes and create one Artifact row; commit by default, or flush for a caller-owned transaction.

    User uploads (/v1/file/upload) and inbound channel attachments
    (core/channels/inbound) share this entry point, avoiding copy-pasted
    storage_key assembly + Artifact creation in two places. Leaving
    ``parsed_text`` empty means it will be lazily backfilled later.
    """
    from core.db.personal_file_names import check_name

    check_name(db, user_id, user_folder_id, filename)
    artifact_id = artifact_id or f"ua_{uuid.uuid4().hex[:16]}"
    storage_key = artifact_upload_key(user_id, artifact_id, filename)
    from core.files.storage import get_storage

    storage_url = get_storage().upload_bytes(content, storage_key)
    meta: Dict[str, Any] = {"source": source}
    if extra:
        meta.update(extra)
    if meta.get("upload_fingerprint"):
        meta["upload_storage_key"] = storage_key
    artifact = ArtifactModel(
        artifact_id=artifact_id,
        chat_id=chat_id,
        user_id=user_id,
        user_folder_id=user_folder_id,
        type="other",
        title=filename,
        filename=filename,
        size_bytes=len(content),
        mime_type=mime_type or "application/octet-stream",
        storage_key=storage_key,
        storage_url=storage_url,
        parsed_text=parsed_text or None,
        extra_data=meta,
    )
    db.add(artifact)
    if commit:
        db.commit()
        db.refresh(artifact)
    else:
        db.flush()
    return artifact


class ArtifactService:
    """Service for artifact management."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = ArtifactRepository(db)

    def create_artifact(
        self,
        user_id: str,
        artifact_type: str,
        title: str,
        filename: str,
        size_bytes: int,
        mime_type: str,
        storage_key: str,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new artifact."""
        artifact_data = {
            "artifact_id": f"artifact_{uuid.uuid4().hex[:16]}",
            "user_id": user_id,
            "chat_id": chat_id,
            "type": artifact_type,
            "title": title,
            "filename": filename,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "storage_key": storage_key,
        }

        artifact = self.repo.create(artifact_data)

        return {
            "artifact_id": artifact.artifact_id,
            "type": artifact.type,
            "title": artifact.title,
            "filename": artifact.filename,
            "created_at": artifact.created_at.isoformat(),
        }

    def get_artifact(self, artifact_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Get artifact with ownership check."""
        artifact = self.repo.get_by_id(artifact_id)

        if not artifact or artifact.user_id != user_id:
            return None

        return {
            "artifact_id": artifact.artifact_id,
            "type": artifact.type,
            "title": artifact.title,
            "filename": artifact.filename,
            "storage_key": artifact.storage_key,
            "size_bytes": artifact.size_bytes,
            "mime_type": artifact.mime_type,
            "created_at": artifact.created_at.isoformat(),
        }


# ── AI-generated artifact persistence (used by chat route + routing) ──────────
# Lives in core so tools and routing can persist artifacts without importing an API route.


def _free_artifact_name(
    db: Session, user_id: str, fields: dict, filename: str, batch: set, *, strict: bool = False
) -> str:
    """First free ``name`` / ``name (1).ext``… in the target folder.

    A generated file that collides with an existing one is kept under a
    numbered name, like personal uploads, instead of failing the whole run.
    ``batch`` holds names already handed out in this call (not yet flushed).
    The edition decides whether strict persistence must keep the requested name.
    """
    from fastapi import HTTPException
    from core.db.personal_file_names import conflict, numbered_names

    target = artifact_name_target(user_id, fields, strict)
    if target is None:
        return filename
    owner, folder_id, check_name = target
    for candidate in numbered_names(filename):
        key = (check_name, owner, folder_id, candidate)
        if key in batch:
            continue
        try:
            check_name(db, owner, folder_id, candidate)
        except HTTPException as error:
            if error.status_code != 409:
                raise
            continue
        batch.add(key)
        if candidate != filename:
            logger.info("artifact_name_renamed: %s -> %s", filename, candidate)
        return candidate
    raise conflict()


def persist_artifacts(
    db: Session,
    user_id: str,
    chat_id: str,
    collected: list,
    *,
    scope: Optional["ProjectScope"] = None,
    commit: bool = True,
    strict: bool = False,
) -> list[dict]:
    """Batch-insert AI-generated artifacts into the Artifact DB table.

    Returns the inputs that were newly inserted, each with ``filename`` set to the
    name the row was stored under (already-known ids are skipped and not returned).

    Strict mode: callers MUST pass only the list of files the agent
    explicitly pinned via ``pin_to_workspace`` (e.g. the result of
    ``core.llm.runtime.workspace.get_pinned()``). Intermediate tool outputs that
    the agent did not pin are deliberately NOT persisted — the user's
    "我的空间" should only show deliverables, not every transient docx
    produced by `word_replace_text` etc.

    User-uploaded files take a separate path (`api/routes/v1/file_upload.py`,
    extra_data.source = "user_upload") and never go through this helper.

    Project-mode auto-placement is delegated to the edition scope seam. The
    shared implementation only knows the personal folder field; commercial
    ownership columns are supplied by the enterprise module.

    **Important**: ``scope`` must be constructed explicitly by the caller from
    the workflow context and passed in. This function no longer reads the
    ContextVar — on the old path, by the time chats.py made the wrap-up call,
    workflow.py's finally had already reset the ContextVar, causing project
    outputs to land in the wrong scope.
    """
    from core.files.artifacts.local_reference import is_local_file_ref

    # Local project cards are live file references, not MySpace storage objects.
    collected = [a for a in collected if not is_local_file_ref(a.get("file_id", ""))]
    if not collected:
        return []

    scope_fields = artifact_scope_fields(scope)

    all_fids = [a["file_id"] for a in collected if a.get("file_id")]
    existing_ids = (
        set(
            r[0]
            for r in db.query(ArtifactModel.artifact_id)
            .filter(ArtifactModel.artifact_id.in_(all_fids))
            .all()
        )
        if all_fids
        else set()
    )
    batch_names: set = set()
    inserted: list[dict] = []
    for art in collected:
        art_id = art.get("file_id", "")
        if not art_id or art_id in existing_ids:
            continue
        mime = art.get("mime_type", "application/octet-stream")
        try:
            storage_key = (
                require_artifact_storage_key(art_id, art.get("storage_key"))
            )
            # The pin tool (strict) reports a team clash back to the model so it
            # edits the existing team file; everywhere else a clash must not
            # fail the run, so the new file takes the next free name.
            filename = _free_artifact_name(
                db, user_id, scope_fields, art.get("name", ""), batch_names,
                strict=strict,
            )
            db.add(
                ArtifactModel(
                    artifact_id=art_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    type=infer_artifact_type(mime),
                    title=filename,
                    filename=filename,
                    size_bytes=max(art.get("size", 0) or 0, 1),
                    mime_type=mime,
                    storage_key=storage_key,
                    storage_url=art.get("url", ""),
                    extra_data={"source": "ai_generated", "tool_name": art.get("tool_name", "")},
                    **scope_fields,
                )
            )
            existing_ids.add(art_id)
            inserted.append({**art, "filename": filename, "storage_key": storage_key})
        except Exception as e:
            if strict:
                if commit:
                    db.rollback()
                raise
            logger.warning("artifact_db_insert_failed: %s", e)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception as e:
        from fastapi import HTTPException

        if strict or isinstance(e, HTTPException):
            if commit:
                db.rollback()
            raise
        logger.warning("artifact_db_commit_failed: %s", e)
        if commit:
            db.rollback()
            return []
        else:
            raise
    return inserted


def _delivery_location(db: Session, scope: Optional["ProjectScope"]) -> tuple[str, str]:
    """(space id, folder path) a run's deliverables belong in: the project folder, else My Space root."""
    if scope is None:
        return "personal", ""
    from core.db.models.project import Project
    from core.files.spaces.projects import project_location

    project = db.get(Project, scope.project_id)
    if project is None:
        raise ValueError("项目不存在")
    space, path, _ = project_location(db, project)
    return space, path


def copy_deliverables_to_space(
    db: Session,
    user_id: str,
    delivered: list,
    *,
    scope: Optional["ProjectScope"] = None,
) -> list[dict]:
    """Put newly pinned deliverables into the file space, as uploads are.

    Since My Space lists the space directory in object storage, an Artifact row alone
    is only the chat's snapshot and never shows up there. Like ``/v1/file/upload``,
    the space gets its own editable copy (server-side when the bytes are in object
    storage). Skipped for desktop local mode (deliveries are live references to the
    original), for files that already sit in a mounted space (written under
    ``/myspace``/team mounts), and for callers without a personal space.
    Returns ``[{file_id, error}]`` for deliverables that could not be copied.
    """
    from core.config.local_mode import local_mode_enabled

    if not delivered or local_mode_enabled():
        return []
    from fastapi import HTTPException
    from core.db.personal_file_names import numbered_names
    from core.files.spaces.access import resolve_grants
    from core.files.spaces.files import SpaceFiles
    from core.files.storage import get_storage

    try:
        grants = resolve_grants(db, user_id)
    except HTTPException:
        return []
    if not grants:
        return []
    mounts = tuple(g.mount_path.rstrip("/") + "/" for g in grants)
    try:
        space, directory = _delivery_location(db, scope)
    except (HTTPException, ValueError) as exc:
        logger.warning("deliverable_space_location_failed: %s", getattr(exc, "detail", exc))
        return [{"file_id": a.get("file_id"), "error": "项目未绑定文件目录，未保存到空间"} for a in delivered]

    files = SpaceFiles(db, user_id, get_storage())
    failures: list[dict] = []
    for art in delivered:
        src_path = str(art.get("src_path") or "")
        if src_path and src_path.startswith(mounts):
            continue  # already a space file
        try:
            name = next(
                candidate for candidate in numbered_names(art["filename"])
                if not files.occupied(space, f"{directory}/{candidate}" if directory else candidate)
            )
            path = f"{directory}/{name}" if directory else name
            local_path = art.get("local_path")
            if local_path and os.path.isfile(local_path):
                with open(local_path, "rb") as handle:
                    files.write(space, path, handle.read())
            else:
                files.copy_from(space, path, art["storage_key"], max(art.get("size", 0) or 0, 1))
        except (HTTPException, OSError, StopIteration, KeyError) as exc:
            logger.warning("deliverable_space_copy_failed: %s", type(exc).__name__)
            message = exc.detail if isinstance(exc, HTTPException) else "保存到空间失败"
            failures.append({"file_id": art.get("file_id"), "error": message})
    return failures
