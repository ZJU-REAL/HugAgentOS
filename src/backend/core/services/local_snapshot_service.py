"""Write-back snapshots + rollback for desktop local files (ticket #08).

Before the agent overwrites a file inside a local project, we copy the current
content into a snapshot store (``~/.hugagent/local_snapshots``). The user can then
roll the file back to its previous version. Best-effort and non-fatal: a snapshot
failure never blocks the write.

Edition-agnostic shared ``core`` module, active only in the desktop local backend
(the callers gate on ``local_mode_enabled()``); cloud/web never snapshots.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_LOCK = threading.Lock()
_MAX_PER_FILE = 10


def _data_dir() -> Path:
    return Path(os.getenv("HUGAGENT_HOME", str(Path.home() / ".hugagent"))).expanduser()


def _snap_root() -> Path:
    return _data_dir() / "local_snapshots"


def _key(real_path: str) -> str:
    return hashlib.sha256(real_path.encode("utf-8")).hexdigest()[:16]


def _dir_for(real_path: str) -> Path:
    return _snap_root() / _key(real_path)


def snapshot(real_path: str) -> Optional[str]:
    """Copy the current content of ``real_path`` into the snapshot store.

    Returns the snapshot file path, or ``None`` if there was nothing to back up
    (new file) or on any error. Never raises.
    """
    try:
        real = os.path.realpath(os.path.expanduser(real_path))
        if not os.path.isfile(real):
            return None
        with _LOCK:
            d = _dir_for(real)
            d.mkdir(parents=True, exist_ok=True)
            (d / "path.txt").write_text(real, encoding="utf-8")
            stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
            dest = d / f"{stamp}.bak"
            shutil.copy2(real, dest)
            _prune(d)
        return str(dest)
    except Exception:
        return None


def _prune(d: Path) -> None:
    backups = sorted(d.glob("*.bak"))
    for old in backups[:-_MAX_PER_FILE]:
        try:
            old.unlink()
        except OSError:
            pass


def list_snapshots(real_path: str) -> List[str]:
    real = os.path.realpath(os.path.expanduser(real_path))
    d = _dir_for(real)
    if not d.is_dir():
        return []
    return [p.stem for p in sorted(d.glob("*.bak"))]


def rollback(real_path: str) -> bool:
    """Restore ``real_path`` to its most recent snapshot. Returns True on success."""
    real = os.path.realpath(os.path.expanduser(real_path))
    d = _dir_for(real)
    backups = sorted(d.glob("*.bak")) if d.is_dir() else []
    if not backups:
        return False
    try:
        with _LOCK:
            # snapshot the current (post-edit) content first, so a rollback is undoable
            if os.path.isfile(real):
                shutil.copy2(real, d / f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}.bak")
                _prune(d)
                backups = sorted(d.glob("*.bak"))
            shutil.copy2(backups[-2] if len(backups) >= 2 else backups[-1], real)
        return True
    except Exception:
        return False


def list_snapshotted_files() -> List[dict]:
    """All files that currently have snapshots: ``[{path, count}]``."""
    root = _snap_root()
    if not root.is_dir():
        return []
    out: List[dict] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        meta = d / "path.txt"
        backups = list(d.glob("*.bak"))
        if meta.is_file() and backups:
            try:
                out.append({"path": meta.read_text(encoding="utf-8").strip(), "count": len(backups)})
            except OSError:
                pass
    return out


def maybe_snapshot_local(physical_path: str) -> None:
    """Snapshot before a write when in local mode and the target is a local file.

    ``physical_path`` is the model-facing sandbox path (e.g.
    ``/workspace/local/<slug>/x``). Resolves it to the real host file and
    snapshots. No-op outside local mode. Never raises.
    """
    try:
        from core.config.local_mode import local_mode_enabled

        if not local_mode_enabled():
            return
        from core.llm.tools._paths import canonicalize_ws_path

        real = os.path.realpath(canonicalize_ws_path(physical_path))
        snapshot(real)
    except Exception:
        pass


__all__ = ["snapshot", "list_snapshots", "rollback", "maybe_snapshot_local"]
