"""Build + cache tar.gz archives of skill directories for fast sandbox delivery.

Pushing a large skill (e.g. ppt-master = 12k files) into a remote MicroVM
file-by-file is slow — one HTTP write per file. Instead we tar the whole skill
dir ONCE on the backend (cached in-process, reused across every sandbox/session),
push a single blob, and untar it inside the sandbox via one command. This applies
uniformly to built-in skills (source tree) and DB/imported skills (materialized
cache dir). See ``cube_provider._push_skill_dir``.

Cache: keyed by skill_id, process-lifetime. Invalidated by
``cache_refresh.refresh_skill_caches`` after admin skill mutations (which also
resets the loader so the next build re-materializes + re-tars). Built-in skills
change only on backend rebuild/restart, so process-lifetime caching is safe.
"""
from __future__ import annotations

import logging
import os
import tarfile
import threading
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_ARCHIVE_SUBDIR = "skill_archives"
_JUNK_PARTS = {"__pycache__", ".git", ".svn", ".hg"}

_cache: Dict[str, str] = {}  # skill_id -> tar_path
_lock = threading.Lock()


def _archives_dir() -> Path:
    from core.agent_skills.config import get_sandbox_skills_dir

    d = get_sandbox_skills_dir().parent / _ARCHIVE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tar_filter(ti: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    parts = set(ti.name.split("/"))
    if parts & _JUNK_PARTS or ti.name.endswith(".pyc"):
        return None
    return ti


def build_skill_tar(skill_id: str, src: Path) -> Optional[Path]:
    """Build (or return cached) a tar.gz of the skill dir ``src``.

    arcname='.' packs the *contents* of src, so extracting into
    ``/workspace/skills/<id>`` reproduces the original layout. compresslevel=1
    favours speed (SVG/text compress well even at level 1; PNGs barely compress
    at any level). Returns the tar path, or None if src is missing.
    """
    with _lock:
        cached = _cache.get(skill_id)
        if cached and Path(cached).is_file():
            return Path(cached)

    if not src.is_dir():
        return None

    tar_path = _archives_dir() / f"{skill_id}.tgz"
    tmp = tar_path.with_suffix(".tgz.tmp")
    try:
        with tarfile.open(tmp, "w:gz", compresslevel=1) as tf:
            tf.add(str(src), arcname=".", filter=_tar_filter)
        os.replace(tmp, tar_path)  # atomic swap; in-flight readers keep old file
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    with _lock:
        _cache[skill_id] = str(tar_path)
    try:
        size = tar_path.stat().st_size
    except OSError:
        size = -1
    logger.info("[skill_archive] built %s (%d bytes)", tar_path.name, size)
    return tar_path


def clear_cache(skill_id: Optional[str] = None) -> None:
    """Drop the in-memory tar cache (next build_skill_tar rebuilds + overwrites).

    Called after admin skill mutations. The on-disk tar is left to be overwritten
    by the next build (fixed ``<id>.tgz`` filename), so no stale files accumulate.
    """
    with _lock:
        if skill_id is None:
            _cache.clear()
        else:
            _cache.pop(skill_id, None)
