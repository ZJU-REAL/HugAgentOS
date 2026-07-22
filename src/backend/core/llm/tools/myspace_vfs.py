"""Unified mapping layer between MySpace and the sandbox ("cloud computer" infrastructure).

Design goal: make ``/myspace/<folder path>/<filename>`` inside the sandbox a
**faithful, lazily-loaded, bidirectionally synced** view of the user's real
MySpace (the ``artifacts`` table + the ``user_folders`` tree).

- **Path model**: ``/myspace/a/b/c.txt`` maps to the artifact named ``c.txt``
  under the ``a/b`` folder in the UserFolder tree; physically it lands at
  ``/workspace/myspace/{uid}/a/b/c.txt`` in the sandbox, with the backend
  mirror cache at ``{storage}/myspace_cache/{uid}/a/b/c.txt``.
- **Lazy loading**: when Read/Edit/Glob/Grep hit a file missing from the
  sandbox, resolve the artifact by path, download it on demand from object
  storage and materialize it into the sandbox (``materialize_into_sandbox``).
- **Reverse sync**: Write/Edit/Delete/Move write sandbox-side changes back to
  the DB — create ``UserFolder`` rows on demand, update in place or create the
  artifact keyed by ``(user_id, folder_id, filename)``, soft-delete, rename/move.

This module holds **pure resolution + DB sync** logic only; it does not depend
on any specific tool directly, and is shared by the read/edit/write/delete/move
tools to keep DB logic from being duplicated everywhere.

**Project scope**: every function that needs project awareness
takes an explicit ``scope: Optional[ProjectScope]`` parameter. **ContextVar is
no longer used** — ContextVar gets reset across async generator finally
boundaries, which once caused chats.py's finalizing ``_persist_artifacts`` to
leak team-project AI output into the personal MySpace root (trace 9d218075…).
Explicit parameter passing eliminates the timing window at the root: a missing
argument = a parameter error, no longer silently falling through.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.config.settings import settings
from core.llm.tools.edition_myspace_vfs import (
    iter_organization_tree,
    organization_cache_file,
    organization_mutation_blocked,
    organization_scope_id,
    resolve_organization_artifact,
    resolve_organization_folder,
)
from core.services.artifact_edition import personal_artifact_predicates
from core.services.project_scope import ProjectScope

logger = logging.getLogger(__name__)

MYSPACE_LOGICAL = "/myspace"
# See core.sandbox._common.WORKSPACE — honours SCRIPT_RUNNER_WORKSPACE so the
# host (no-Docker) profile materialises myspace files under the real workspace
# root the sidecar validates against, not a literal /workspace.
from core.sandbox._common import WORKSPACE as WORKSPACE_ROOT


def _apply_scope_to_rel(rel: Optional[str], scope: Optional[ProjectScope]) -> Optional[str]:
    """Prefix a relative path with the anchor folder name of the current project scope.

    - no scope / scope without folder_name: return as-is (including None)
    - path already under the project folder: return as-is (avoid double nesting)
    - path is the root ``""``: return the project folder name
    - otherwise: ``"<folder>/<rel>"``

    Every project kind gets the prefix redirect: when the frontend starts a
    project conversation it confines the entry path to the anchor
    folder, but the model may still think in relative names (``foo.txt``);
    here we uniformly re-attach such "bare paths" under the project folder.
    """
    if rel is None:
        return None
    if scope is None:
        return rel
    folder_name = (scope.folder_name or "").strip()
    if not folder_name:
        return rel
    if rel == folder_name or rel.startswith(folder_name + "/"):
        return rel
    if rel == "":
        return folder_name
    return f"{folder_name}/{rel}"


# ──────────────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────────────
def myspace_rel(
    path: str,
    user_id: Optional[str],
    scope: Optional[ProjectScope] = None,
) -> Optional[str]:
    """Normalize a logical/physical myspace path into a relative path (e.g. ``a/b/c.txt``).

    - ``/myspace`` / ``/myspace/`` → ``""`` (root)
    - ``/myspace/a/b.txt`` → ``a/b.txt``
    - ``/workspace/myspace/{uid}/a/b.txt`` → ``a/b.txt``
    - non-myspace path → ``None``

    Project-scope aware: when ``scope`` is non-empty,
    the result is prefixed with the project anchor folder name (not repeated if
    already present). Thus ``/myspace/foo.txt`` in a project conversation
    automatically becomes ``<project folder>/foo.txt``, and every path that
    goes through myspace_rel — sync_upsert / glob / iter_tree etc. — lands in
    the project subtree.
    """
    if not path:
        return None
    p = path.rstrip("/") or path
    rel: Optional[str] = None
    if p == MYSPACE_LOGICAL:
        rel = ""
    elif p.startswith(MYSPACE_LOGICAL + "/"):
        rel = p[len(MYSPACE_LOGICAL) + 1 :]
    elif user_id:
        phys_root = f"{WORKSPACE_ROOT}/myspace/{user_id}"
        if p == phys_root:
            rel = ""
        elif p.startswith(phys_root + "/"):
            rel = p[len(phys_root) + 1 :]
    if rel is None:
        return None
    return _apply_scope_to_rel(rel, scope)


def split_rel(rel: str) -> tuple[list[str], Optional[str]]:
    """Split a relative path into ``(list of folder-name segments, leaf name)``.

    The leaf is interpreted as a "filename"; the root (``""``) returns ``([], None)``.
    Folder paths and file paths are indistinguishable at the pure string level;
    callers decide by semantics:
    - file-like (read/write/edit): leaf = filename, prefix = folder chain.
    - directory-like (list): the whole string is a folder chain — after
      ``split_rel``, merge the leaf back in as well.
    """
    rel = (rel or "").strip("/")
    if not rel:
        return [], None
    parts = [seg for seg in rel.split("/") if seg]
    if not parts:
        return [], None
    return parts[:-1], parts[-1]


# ──────────────────────────────────────────────────────────────────────────
# Folder tree resolution / creation
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class FolderResolve:
    found: bool  # whether every folder on the path exists (meaningful when create=False)
    folder_id: Optional[str]  # None = root directory


def resolve_folder_id(
    db: Any,
    user_id: str,
    folder_names: list[str],
    *,
    create: bool = False,
) -> FolderResolve:
    """Walk the UserFolder tree level by level by name segments and return the final folder_id (None=root).

    With ``create=True``, missing levels are created on demand via UserFolderService (actor=user_id).
    With ``create=False``, any missing level → ``found=False``.
    """
    from core.db.models import UserFolder

    parent_id: Optional[str] = None
    for name in folder_names:
        q = db.query(UserFolder).filter(
            UserFolder.user_id == user_id,
            UserFolder.name == name,
            UserFolder.deleted_at.is_(None),
        )
        if parent_id is None:
            q = q.filter(UserFolder.parent_folder_id.is_(None))
        else:
            q = q.filter(UserFolder.parent_folder_id == parent_id)
        row = q.first()
        if row is not None:
            parent_id = row.folder_id
            continue
        if not create:
            return FolderResolve(found=False, folder_id=parent_id)
        from core.services.user_folder_service import UserFolderService

        res = UserFolderService(db).create_folder(
            user_id=user_id,
            parent_folder_id=parent_id,
            name=name,
            actor=user_id,
        )
        if not res.ok or not res.folder_id:
            logger.warning("[myspace] 创建文件夹失败 name=%s: %s", name, res.message)
            return FolderResolve(found=False, folder_id=parent_id)
        parent_id = res.folder_id
    return FolderResolve(found=True, folder_id=parent_id)


def resolve_file_id(
    user_id: str,
    logical_path: str,
    scope: Optional[ProjectScope] = None,
) -> Optional[str]:
    """Resolve a ``/myspace`` file path to an artifact_id (file_id), ``None`` if absent.

    Used by Read to fall back to ``fetch_parsed_text`` in the binary office
    document scenario. Edition-specific project scopes are resolved through a
    separate implementation that is absent from Community Edition.
    """
    if not user_id:
        return None
    rel = myspace_rel(logical_path, user_id, scope)
    if not rel:
        return None
    folder_names, filename = split_rel(rel)
    if not filename:
        return None
    try:
        from core.db.engine import SessionLocal
    except Exception:  # noqa: BLE001
        return None
    db = SessionLocal()
    try:
        organization_folder = resolve_organization_folder(db, scope, folder_names, create=False)
        if organization_folder is None:
            fr = resolve_folder_id(db, user_id, folder_names, create=False)
            if not fr.found:
                return None
            art = resolve_artifact(db, user_id, fr.folder_id, filename)
        else:
            if not organization_folder.found:
                return None
            art = resolve_organization_artifact(db, scope, organization_folder.folder_id, filename)
        return art.artifact_id if art is not None else None
    finally:
        db.close()


def resolve_artifact(
    db: Any,
    user_id: str,
    folder_id: Optional[str],
    filename: str,
) -> Any:
    """Locate the latest live artifact by filename under the given personal folder (None=root).

    Explicitly excludes artifacts attached to a project (those go through the
    project toolchain and are not in the MySpace view).
    """
    from core.db.models import Artifact

    q = db.query(Artifact).filter(
        Artifact.user_id == user_id,
        Artifact.filename == filename,
        *personal_artifact_predicates(Artifact),
        Artifact.deleted_at.is_(None),
    )
    if folder_id is None:
        q = q.filter(Artifact.user_folder_id.is_(None))
    else:
        q = q.filter(Artifact.user_folder_id == folder_id)
    return q.order_by(Artifact.created_at.desc()).first()


# ──────────────────────────────────────────────────────────────────────────
# Cache mirroring (subdirectory-aware)
# ──────────────────────────────────────────────────────────────────────────
def myspace_cache_file(user_id: str, rel: str) -> Path:
    """Subdirectory-aware backend mirror cache file path (``myspace_cache/{uid}/<rel>``)."""
    from core.sandbox._common import myspace_cache_dir

    return myspace_cache_dir(user_id) / rel


def mirror_to_cache(
    user_id: str,
    rel: str,
    content: bytes,
    *,
    scope: Optional[ProjectScope] = None,
) -> None:
    """Mirror bytes into the edition-appropriate backend cache."""
    try:
        fp = organization_cache_file(scope, rel) or myspace_cache_file(user_id, rel)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace] mirror_to_cache 失败 rel=%s: %s", rel, exc)


def _guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


# ──────────────────────────────────────────────────────────────────────────
# Lazy loading: materialize artifacts into the sandbox on demand
# ──────────────────────────────────────────────────────────────────────────
async def materialize_into_sandbox(
    provider: Any,
    chat_id: Optional[str],
    user_id: Optional[str],
    logical_path: str,
    *,
    scope: Optional[ProjectScope] = None,
) -> Optional[bytes]:
    """When the sandbox lacks the file, resolve the artifact by myspace path and materialize it into the sandbox.

    Returns the materialized bytes; returns ``None`` when unresolvable (path is
    not myspace / folder missing / no such file). This is the core entry point
    of "lazy loading + on-demand materialization".

    NOTE: the ``chat_id`` parameter is actually the *sandbox session id* (callers
    pass the already-resolved ``_sess``); it is only used by
    ``provider.put_file`` to select the sandbox, not a DB dimension.

    Edition-specific scopes resolve through the edition seam and use their own
    cache location. Community Edition only executes the personal branch.
    """
    if not user_id:
        return None
    rel = myspace_rel(logical_path, user_id, scope)
    if rel is None or rel == "":
        return None
    folder_names, filename = split_rel(rel)
    if not filename:
        return None

    try:
        from core.db.engine import SessionLocal
        from core.storage import get_storage
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace] materialize deps 不可用: %s", exc)
        return None

    db = SessionLocal()
    try:
        organization_folder = resolve_organization_folder(db, scope, folder_names, create=False)
        if organization_folder is None:
            fr = resolve_folder_id(db, user_id, folder_names, create=False)
            if not fr.found:
                return None
            art = resolve_artifact(db, user_id, fr.folder_id, filename)
        else:
            if not organization_folder.found:
                return None
            art = resolve_organization_artifact(db, scope, organization_folder.folder_id, filename)
        if art is None:
            return None
        storage_key = str(art.storage_key)
    finally:
        db.close()

    try:
        data = get_storage().download_bytes(storage_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace] download_bytes 失败 key=%s: %s", storage_key, exc)
        return None

    # Plan F: with the bind mount on, writing the local cache is writing the same inode
    # as /workspace/myspace/{uid}/ inside the sandbox — no HTTP PUT needed to move the
    # bytes into the sandbox, saving a round trip.
    # With the flag off, take the old path: PUT into the sandbox first, then mirror the
    # local cache.
    # NOTE: the bind mount is an OpenSandbox-exclusive capability; other providers like
    # cube / script_runner have no mount even with the flag on. Skipping put_file would
    # mean the file never reaches the sandbox — a subsequent Write misjudges it as
    # "new" due to the get_file miss, bypassing the read-before-write protection
    # (actual incident: a docx artifact was overwritten in place with plain text).
    # So the provider must be validated as well.
    bind_mount_active = (
        settings.sandbox.opensandbox_myspace_bind_mount_enabled
        and settings.sandbox.provider == "opensandbox"
    )
    if not bind_mount_active:
        physical = f"{WORKSPACE_ROOT}/myspace/{user_id}/{rel}"
        try:
            await provider.put_file(chat_id, physical, data, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[myspace] put_file 自愈失败 %s: %s", physical, exc)
            # Even if refilling the sandbox fails, hand the bytes back to the caller
            # (at least this round can read them)
    mirror_to_cache(user_id, rel, data, scope=scope)
    logger.info(
        "[myspace] materialized %s (artifact, %d bytes, scope=%s)",
        logical_path,
        len(data),
        "organization" if organization_scope_id(scope) else "personal",
    )
    return data


# ──────────────────────────────────────────────────────────────────────────
# Reverse sync: create / update (folder-aware, replaces the old upsert_myspace_artifact)
# ──────────────────────────────────────────────────────────────────────────
def sync_upsert(
    *,
    user_id: str,
    chat_id: Optional[str],
    logical_path: str,
    content: bytes,
    scope: Optional[ProjectScope] = None,
) -> Optional[dict[str, Any]]:
    """Reverse-sync a write to ``/myspace/<folder>/<file>`` into MySpace.

    - Create the UserFolder chain along the path on demand.
    - Look up the existing artifact by ``(user_id, folder_id, filename)``: on a
      hit, overwrite in place (same file_id, Canvas/download links unchanged);
      otherwise create a new one and set ``user_folder_id``.
    - Also mirror into myspace_cache (preserving subdirectories).

    Returns an artifact ref (``{file_id, name, url, mime_type, size, storage_key,
    in_place_update}``) or ``None`` (on sync failure the caller should soft-warn,
    not block the write itself).
    """
    if organization_mutation_blocked(scope):
        logger.warning(
            "[myspace] sync_upsert was blocked by the edition scope policy: %s",
            logical_path,
        )
        return None
    rel = myspace_rel(logical_path, user_id, scope)
    if rel is None or rel == "":
        logger.warning("[myspace] sync_upsert 非法路径: %s", logical_path)
        return None
    folder_names, filename = split_rel(rel)
    if not filename:
        return None

    name = filename
    mime = _guess_mime(name)

    # 1. Mirror into the cache first (even if the subsequent DB step fails, the next seed still sees it)
    mirror_to_cache(user_id, rel, content)

    try:
        from core.db.engine import SessionLocal
        from core.db.models import Artifact
        from core.storage import get_storage
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace] sync_upsert deps 不可用: %s", exc)
        return None

    db = SessionLocal()
    try:
        fr = resolve_folder_id(db, user_id, folder_names, create=True)
        folder_id = fr.folder_id
        art = resolve_artifact(db, user_id, folder_id, name)

        # 2a. In-place overwrite of an existing artifact
        if art is not None:
            try:
                get_storage().upload_bytes(content, str(art.storage_key))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[myspace] upload_bytes 失败 %s: %s", art.storage_key, exc)
                return None
            art.size_bytes = max(len(content), 1)
            art.mime_type = mime
            art.updated_at = datetime.now(timezone.utc)
            db.commit()
            logger.info(
                "[myspace] in-place 更新 %s (artifact=%s folder=%s %dB)",
                rel,
                art.artifact_id,
                folder_id,
                len(content),
            )
            return {
                "file_id": art.artifact_id,
                "name": name,
                "url": f"/files/{art.artifact_id}",
                "mime_type": mime,
                "size": len(content),
                "storage_key": art.storage_key,
                "in_place_update": True,
            }

        # 2b. Create a new artifact (into storage + JSON index), then insert the DB row immediately
        try:
            from core.llm.tools._tool_helpers import _store_generated_files
        except Exception as exc:  # noqa: BLE001
            logger.warning("[myspace] _store_generated_files 不可用: %s", exc)
            return None

        refs = _store_generated_files(
            [
                {
                    "name": name,
                    "size": len(content),
                    "content_b64": base64.b64encode(content).decode("ascii"),
                    "mime_type": mime,
                }
            ],
            user_id=user_id,
            source="myspace_sync",
            extra_metadata={"chat_id": chat_id} if chat_id else None,
        )
        if not refs:
            return None
        ref = dict(refs[0])
        ref["in_place_update"] = False
        new_file_id = ref.get("file_id")

        # chat_id may be empty: MySpace files are cross-session and unrelated to a
        # specific chat (the Artifact.chat_id column is nullable, FK ondelete=SET NULL).
        # An early version mistakenly added an `and chat_id` guard, so writing /myspace
        # without a chat context only wrote object storage and never landed a queryable
        # DB row → the file was completely invisible in MySpace. That guard is removed
        # here.
        if new_file_id:
            existing = (
                db.query(Artifact)
                .filter(
                    Artifact.artifact_id == new_file_id,
                )
                .first()
            )
            if existing is None:
                db.add(
                    Artifact(
                        artifact_id=new_file_id,
                        chat_id=chat_id,
                        user_id=user_id,
                        user_folder_id=folder_id,
                        type="other",
                        title=name,
                        filename=name,
                        size_bytes=max(len(content), 1),
                        mime_type=mime,
                        storage_key=ref.get("storage_key") or f"artifacts/{new_file_id}",
                        storage_url=ref.get("url"),
                        extra_data={"source": "myspace_sync"},
                    )
                )
                db.commit()
            else:
                # Row already exists (same-run race) → only patch the folder ownership
                if existing.user_folder_id != folder_id:
                    existing.user_folder_id = folder_id
                    db.commit()
        logger.info(
            "[myspace] 新建 artifact %s (rel=%s folder=%s)",
            new_file_id,
            rel,
            folder_id,
        )
        return ref
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace] sync_upsert 异常: %s", exc)
        db.rollback()
        return None
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────
# Reverse sync: delete (file or folder)
# ──────────────────────────────────────────────────────────────────────────
def sync_delete(
    user_id: str,
    logical_path: str,
    *,
    scope: Optional[ProjectScope] = None,
) -> dict[str, Any]:
    """Soft-delete a file or folder under ``/myspace``.

    Resolve as a "file" first (parent folder + filename); if that misses,
    resolve the whole string as a "folder". Returns
    ``{ok, kind: 'file'|'folder', removed, artifacts_affected?}`` or
    ``{error}``. Also cleans up the myspace_cache mirror.
    """
    if organization_mutation_blocked(scope):
        return {"error": "当前项目范围不支持通过 agent 删除文件"}
    rel = myspace_rel(logical_path, user_id, scope)
    if rel is None or rel == "":
        return {"error": f"不是合法的我的空间路径或不允许删根: {logical_path}"}
    folder_names, leaf = split_rel(rel)
    if not leaf:
        return {"error": f"无法解析删除目标: {logical_path}"}

    try:
        from core.db.engine import SessionLocal
    except Exception as exc:  # noqa: BLE001
        return {"error": f"DB 不可用: {exc}"}

    db = SessionLocal()
    try:
        # 1) Treat as a file: parent folder chain + filename
        fr = resolve_folder_id(db, user_id, folder_names, create=False)
        if fr.found:
            art = resolve_artifact(db, user_id, fr.folder_id, leaf)
            if art is not None:
                art.deleted_at = datetime.now(timezone.utc)
                db.commit()
                _remove_cache(user_id, rel)
                logger.info("[myspace] 软删文件 %s (artifact=%s)", rel, art.artifact_id)
                return {"ok": True, "kind": "file", "removed": rel}

        # 2) Treat as a folder: the whole string is a folder chain
        all_names = folder_names + [leaf]
        fr2 = resolve_folder_id(db, user_id, all_names, create=False)
        if fr2.found and fr2.folder_id:
            from core.services.user_folder_service import UserFolderService

            res, affected = UserFolderService(db).delete_folder(fr2.folder_id, user_id)
            if res.ok:
                _remove_cache(user_id, rel, is_dir=True)
                logger.info(
                    "[myspace] 软删文件夹 %s (folder=%s, %d 文件)",
                    rel,
                    fr2.folder_id,
                    affected,
                )
                return {
                    "ok": True,
                    "kind": "folder",
                    "removed": rel,
                    "artifacts_affected": affected,
                }
            return {"error": res.message}

        return {"error": f"我的空间里找不到 {rel}（文件和文件夹都没匹配）"}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"error": f"删除失败: {exc}"}
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────
# Reverse sync: move / rename (file or folder)
# ──────────────────────────────────────────────────────────────────────────
def sync_move(
    user_id: str,
    src_path: str,
    dst_path: str,
    *,
    scope: Optional[ProjectScope] = None,
) -> dict[str, Any]:
    """Move/rename a file or folder within MySpace.

    - File: change ``filename`` and ``user_folder_id`` (the dst parent folder is
      created on demand). storage_key is unchanged (addressed by artifact_id),
      so download links stay valid.
    - Folder: goes through UserFolderService rename/move.

    Returns ``{ok, kind, src, dst}`` or ``{error}``.
    """
    if organization_mutation_blocked(scope):
        return {"error": "当前项目范围不支持通过 agent 移动文件"}
    src_rel = myspace_rel(src_path, user_id, scope)
    dst_rel = myspace_rel(dst_path, user_id, scope)
    if not src_rel:
        return {"error": f"源不是合法我的空间路径: {src_path}"}
    if not dst_rel:
        return {"error": f"目标不是合法我的空间路径: {dst_path}"}

    src_dirs, src_leaf = split_rel(src_rel)
    dst_dirs, dst_leaf = split_rel(dst_rel)
    if not src_leaf or not dst_leaf:
        return {"error": "move 的源/目标必须指到具体文件或文件夹"}

    try:
        from core.db.engine import SessionLocal
        from core.services.user_folder_service import UserFolderService
    except Exception as exc:  # noqa: BLE001
        return {"error": f"DB 不可用: {exc}"}

    db = SessionLocal()
    try:
        # 1) Treat as a file
        src_fr = resolve_folder_id(db, user_id, src_dirs, create=False)
        if src_fr.found:
            art = resolve_artifact(db, user_id, src_fr.folder_id, src_leaf)
            if art is not None:
                dst_fr = resolve_folder_id(db, user_id, dst_dirs, create=True)
                # Same name already exists at the destination → refuse (avoid silent overwrite)
                if resolve_artifact(db, user_id, dst_fr.folder_id, dst_leaf) is not None:
                    return {"error": f"目标已存在同名文件: {dst_rel}"}
                art.user_folder_id = dst_fr.folder_id
                art.filename = dst_leaf
                art.title = dst_leaf
                art.updated_at = datetime.now(timezone.utc)
                db.commit()
                _remove_cache(user_id, src_rel)
                logger.info("[myspace] 移动文件 %s → %s", src_rel, dst_rel)
                return {"ok": True, "kind": "file", "src": src_rel, "dst": dst_rel}

        # 2) Treat as a folder
        src_all = src_dirs + [src_leaf]
        src_folder = resolve_folder_id(db, user_id, src_all, create=False)
        if src_folder.found and src_folder.folder_id:
            svc = UserFolderService(db)
            dst_parent = resolve_folder_id(db, user_id, dst_dirs, create=True)
            # Move to the destination parent first, then rename as needed
            mv = svc.move_folder(src_folder.folder_id, dst_parent.folder_id, user_id)
            if not mv.ok:
                return {"error": mv.message}
            if dst_leaf != src_leaf:
                rn = svc.rename_folder(src_folder.folder_id, dst_leaf, user_id)
                if not rn.ok:
                    return {"error": rn.message}
            _remove_cache(user_id, src_rel, is_dir=True)
            logger.info("[myspace] 移动文件夹 %s → %s", src_rel, dst_rel)
            return {"ok": True, "kind": "folder", "src": src_rel, "dst": dst_rel}

        return {"error": f"我的空间里找不到源 {src_rel}"}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"error": f"移动失败: {exc}"}
    finally:
        db.close()


def sync_mkdir(
    user_id: str,
    logical_path: str,
    *,
    scope: Optional[ProjectScope] = None,
) -> dict[str, Any]:
    """Create a folder in MySpace, including any missing parent folders along the path (``mkdir -p`` semantics, idempotent).

    MySpace is a DB tree (``UserFolder`` rows); folders and files live in
    different tables. This function goes through exactly the same
    ``resolve_folder_id(create=True)`` chain used internally by Write/Move,
    so behavior is consistent and risk is low.

    Returns ``{ok, kind:"folder", path, created}`` or ``{error}``.
    ``created=False`` means the folder already existed (idempotent success,
    not an error).
    """
    if organization_mutation_blocked(scope):
        return {"error": "当前项目范围不支持通过 agent 创建文件夹"}
    rel = myspace_rel(logical_path, user_id, scope)
    if rel is None:
        return {"error": f"不是合法我的空间路径: {logical_path}"}
    rel = rel.strip("/")
    if not rel:
        return {"error": "不能创建我的空间根目录本身；请指定一个子文件夹路径"}
    dirs, leaf = split_rel(rel)
    folder_names = dirs + [leaf] if leaf else dirs
    if not folder_names:
        return {"error": "文件夹路径为空"}

    try:
        from core.db.engine import SessionLocal
    except Exception as exc:  # noqa: BLE001
        return {"error": f"DB 不可用: {exc}"}

    db = SessionLocal()
    try:
        existed = resolve_folder_id(db, user_id, folder_names, create=False)
        already = existed.found and existed.folder_id is not None
        # create=True internally does UserFolderService.create_folder + commit level by level for missing layers
        fr = resolve_folder_id(db, user_id, folder_names, create=True)
        if not fr.found or fr.folder_id is None:
            return {"error": f"创建文件夹失败: {rel}"}
        logger.info("[myspace] 创建文件夹 %s (created=%s)", rel, not already)
        return {
            "ok": True,
            "kind": "folder",
            "path": rel,
            "created": not already,
        }
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"error": f"创建文件夹失败: {exc}"}
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────
# Tree traversal / Glob / bulk materialization (so Glob / Grep reflect the real MySpace)
# ──────────────────────────────────────────────────────────────────────────
def iter_tree(
    db: Any,
    user_id: str,
    root_folder_id: Optional[str],
) -> list[tuple[str, Any]]:
    """Recursively traverse all artifacts under a folder (None=root), returning ``[(rel_path, art)]``.

    ``rel_path`` is relative to ``root`` (including the subfolder prefix).
    """
    from core.db.models import Artifact, UserFolder

    out: list[tuple[str, Any]] = []
    stack: list[tuple[Optional[str], str]] = [(root_folder_id, "")]
    guard = 0
    while stack and guard < 5000:
        guard += 1
        fid, prefix = stack.pop()
        fq = db.query(Artifact).filter(
            Artifact.user_id == user_id,
            *personal_artifact_predicates(Artifact),
            Artifact.deleted_at.is_(None),
        )
        fq = fq.filter(
            Artifact.user_folder_id.is_(None) if fid is None else Artifact.user_folder_id == fid
        )
        for art in fq.all():
            if art.filename:
                out.append((prefix + art.filename, art))
        sq = db.query(UserFolder).filter(
            UserFolder.user_id == user_id,
            UserFolder.deleted_at.is_(None),
        )
        sq = sq.filter(
            UserFolder.parent_folder_id.is_(None)
            if fid is None
            else UserFolder.parent_folder_id == fid
        )
        for sub in sq.all():
            stack.append((sub.folder_id, f"{prefix}{sub.name}/"))
    return out


def _resolve_root(
    db: Any,
    user_id: str,
    root_logical: str,
    scope: Optional[ProjectScope],
) -> Optional[FolderResolve]:
    """Resolve a directory-like logical path to the current scope root."""
    rel = myspace_rel(root_logical, user_id, scope)
    if rel is None:
        return None
    rel = rel.strip("/")
    names = [s for s in rel.split("/") if s] if rel else []
    organization_folder = resolve_organization_folder(db, scope, names, create=False)
    if organization_folder is not None:
        return organization_folder
    return resolve_folder_id(db, user_id, names, create=False)


def glob_tree(
    user_id: str,
    root_logical: str,
    pattern: str,
    scope: Optional[ProjectScope] = None,
) -> Optional[list[str]]:
    """Glob-match files in the current MySpace anchor folder tree, returning a
    list of ``/myspace/...`` logical paths.

    - contains ``**`` → match the full relative path across subdirectories.
    - otherwise → match only filenames at the ``root`` level.
    Non-myspace paths return ``None`` (the caller falls back to sandbox find).
    """
    import fnmatch

    rel0 = myspace_rel(root_logical, user_id, scope)
    if rel0 is None:
        return None
    base = ("/myspace/" + rel0).rstrip("/") if rel0 else "/myspace"

    try:
        from core.db.engine import SessionLocal
    except Exception:  # noqa: BLE001
        return None
    db = SessionLocal()
    try:
        fr = _resolve_root(db, user_id, root_logical, scope)
        if fr is None or not fr.found:
            return []
        organization_entries = iter_organization_tree(db, scope, fr.folder_id)
        if organization_entries is None:
            entries = iter_tree(db, user_id, fr.folder_id)
        else:
            entries = organization_entries
    finally:
        db.close()

    recursive = "**" in pattern
    pat = pattern.replace("**", "*")
    hits: list[str] = []
    for rel_path, _art in entries:
        if recursive:
            if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(rel_path, pat.lstrip("*/")):
                hits.append(f"{base}/{rel_path}")
        else:
            if "/" in rel_path:
                continue  # non-recursive only looks at the current level
            if fnmatch.fnmatch(rel_path, pat):
                hits.append(f"{base}/{rel_path}")
    return hits


# grep is only meaningful for text; binaries like docx/xlsx/pdf/png/zip can't be searched
# even when materialized — they only slow things down and flood output. materialize_tree
# pulls only these text-like extensions.
_TEXT_EXT = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".htm",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".sh",
    ".bash",
    ".sql",
    ".css",
    ".scss",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".rb",
    ".php",
    ".env",
}


def _is_text_name(name: str) -> bool:
    i = name.rfind(".")
    return i != -1 and name[i:].lower() in _TEXT_EXT


async def materialize_tree(
    provider: Any,
    chat_id: Optional[str],
    user_id: Optional[str],
    root_logical: str,
    *,
    max_files: int = 60,
    scope: Optional[ProjectScope] = None,
) -> int:
    """Bulk-materialize the **text** files under a MySpace subtree into the sandbox concurrently, for Grep to search.

    Key performance constraints (fixing the "large-space search blowup" issue):
    - Pull only text-like extensions (binaries can't be grepped; skip them).
    - Concurrent downloads (semaphore-throttled), no longer one-by-one serial +
      get_file probes spamming 404s.
    - ``max_files`` defaults to 60, truncating beyond that (returns the actual
      materialized count; the caller may surface a hint).

    NOTE: the ``chat_id`` parameter is actually the *sandbox session id* (callers
    pass the already-resolved ``_sess``); it is only used by
    ``provider.put_file`` to select the sandbox, not a DB dimension.
    """
    if not user_id:
        return 0
    rel0 = myspace_rel(root_logical, user_id, scope)
    if rel0 is None:
        return 0

    try:
        from core.db.engine import SessionLocal
        from core.storage import get_storage
    except Exception:  # noqa: BLE001
        return 0

    db = SessionLocal()
    try:
        fr = _resolve_root(db, user_id, root_logical, scope)
        if fr is None or not fr.found:
            return 0
        organization_entries = iter_organization_tree(db, scope, fr.folder_id)
        if organization_entries is None:
            raw_entries = iter_tree(db, user_id, fr.folder_id)
        else:
            raw_entries = organization_entries
        entries = [(rp, art) for rp, art in raw_entries if _is_text_name(rp)]
    finally:
        db.close()

    total_text = len(entries)
    entries = entries[:max_files]
    base_rel = rel0.strip("/")
    storage = get_storage()
    sem = asyncio.Semaphore(8)
    done = 0

    async def _pull(rel_path: str, art: Any) -> None:
        nonlocal done
        full_rel = f"{base_rel}/{rel_path}" if base_rel else rel_path
        physical = f"{WORKSPACE_ROOT}/myspace/{user_id}/{full_rel}"
        async with sem:
            try:
                data = await asyncio.to_thread(storage.download_bytes, str(art.storage_key))
                await provider.put_file(chat_id, physical, data, user_id=user_id)
                mirror_to_cache(user_id, full_rel, data, scope=scope)
                done += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("[myspace] materialize_tree 跳过 %s: %s", full_rel, exc)

    if entries:
        await asyncio.gather(*(_pull(rp, a) for rp, a in entries))
    logger.info(
        "[myspace] materialize_tree %s → %d 文本文件物化（候选文本 %d）",
        root_logical,
        done,
        total_text,
    )
    return done


def _remove_cache(user_id: str, rel: str, *, is_dir: bool = False) -> None:
    """Clean up the myspace_cache mirror (a file or a whole directory); failures only warn."""
    try:
        import shutil

        fp = myspace_cache_file(user_id, rel)
        if is_dir and fp.is_dir():
            shutil.rmtree(fp, ignore_errors=True)
        elif fp.exists():
            fp.unlink()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace] 清理缓存失败 rel=%s: %s", rel, exc)
