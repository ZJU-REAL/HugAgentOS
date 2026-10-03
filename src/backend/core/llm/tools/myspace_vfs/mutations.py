# Personal-space reverse synchronization.
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
from core.content.artifact_refs import require_artifact_storage_key
from core.llm.tools.myspace_vfs.paths import (
    MYSPACE_LOGICAL,
    WORKSPACE_ROOT,
    FolderResolve,
    _guess_mime,
    mirror_to_cache,
    myspace_cache_file,
    myspace_rel,
    resolve_artifact,
    resolve_file_id,
    resolve_folder_id,
    split_rel,
)

# See core.sandbox._common.WORKSPACE — honours SCRIPT_RUNNER_WORKSPACE so the
# host (no-Docker) profile materialises myspace files under the real workspace
# root the sidecar validates against, not a literal /workspace.
from core.sandbox._common import WORKSPACE as WORKSPACE_ROOT


def sync_upsert(
    *,
    user_id: str,
    chat_id: Optional[str],
    logical_path: str,
    content: bytes,
    scope: Optional[ProjectScope] = None,
    mirror: bool = True,
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

    # 1. Mirror into the cache first (even if the subsequent DB step fails, the next seed still sees it).
    #    ``mirror=False`` means the bytes were just read from that very cache file — writing
    #    them back would only produce another filesystem event for the watcher to judge.
    if mirror:
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
                        storage_key=require_artifact_storage_key(
                            new_file_id, ref.get("storage_key")
                        ),
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
