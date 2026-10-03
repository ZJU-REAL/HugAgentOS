# Sandbox materialization and tree traversal.
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
    # ``**/`` 按惯例也匹配零层目录（pathlib / Claude Code 的 Glob 都是如此），所以再拿
    # 去掉开头 ``*/`` 的形式比一次，让 ``**/*.txt`` 也命中根下的文件。这里必须切片而不是
    # lstrip("*/") —— 后者按字符集剥，会把 ``*/*.txt`` 一路啃成 ``.txt``，于是根下的文件
    # 一个都匹配不上，而调用方只会看到一个空列表、不会看到任何错误。
    flat = pat[2:] if pat.startswith("*/") else pat
    hits: list[str] = []
    for rel_path, _art in entries:
        if recursive:
            if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(rel_path, flat):
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
