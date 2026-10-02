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
