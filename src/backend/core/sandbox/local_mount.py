"""Desktop local-project workspace mounting (ticket #04).

A local project is a real folder on the user's computer. The no-Docker host
subprocess sandbox works out of a single workspace root
(``SCRIPT_RUNNER_WORKSPACE``, e.g. ``~/.hugagent/workspace``). To let the agent
reach a local folder without changing the per-request cwd machinery, we expose
it under a stable ``local/<slug>`` namespace inside that root via a symlink (or,
on Windows, a directory junction — refined in ticket #10). The model then works
in ``/workspace/local/<slug>/`` and every read/write/exec lands in the real
folder.

Edition-agnostic shared ``core`` module; only ever invoked for local projects on
the desktop local backend, so it is inert on the cloud/web deployment.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional


def _workspace_root() -> str:
    # Read lazily so tests / callers can override SCRIPT_RUNNER_WORKSPACE.
    return os.getenv("SCRIPT_RUNNER_WORKSPACE", "/workspace")


def local_link_path(slug: str, *, workspace_root: Optional[str] = None) -> str:
    """The in-sandbox path a local project is mounted at: ``<root>/local/<slug>``."""
    root = workspace_root or _workspace_root()
    return os.path.join(root, "local", slug)


def ensure_local_project_link(
    slug: str, real_path: str, *, workspace_root: Optional[str] = None
) -> str:
    """Make ``<root>/local/<slug>`` point at ``real_path``. Idempotent.

    Returns the link path. Raises ``ValueError`` if the target folder does not
    exist (an authorized folder that was moved/deleted — the caller surfaces a
    re-authorization prompt). Repoints the link if it already exists but targets
    a different path.
    """
    if not slug:
        raise ValueError("local project slug is required")
    real = os.path.abspath(os.path.expanduser((real_path or "").strip()))
    if not real or not os.path.isdir(real):
        raise ValueError(f"本地项目文件夹不存在或不可访问：{real_path}")

    link = local_link_path(slug, workspace_root=workspace_root)
    os.makedirs(os.path.dirname(link), exist_ok=True)

    if os.path.islink(link):
        if os.path.realpath(link) == os.path.realpath(real):
            return link
        os.unlink(link)
    elif os.path.exists(link):
        # A real dir/file squatting the slug name — refuse rather than clobber.
        raise ValueError(f"工作区中 {link} 已被占用，无法挂载本地项目")

    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform
        # Windows without symlink privilege → junction fallback (ticket #10).
        if os.name == "nt":
            _windows_junction(link, real)
        else:
            raise ValueError(f"无法挂载本地项目目录：{exc}") from exc
    return link


def remove_local_project_link(slug: str, *, workspace_root: Optional[str] = None) -> None:
    """Remove the ``local/<slug>`` link if present. Never touches the real folder."""
    if not slug:
        return
    link = local_link_path(slug, workspace_root=workspace_root)
    try:
        if os.path.islink(link):
            os.unlink(link)
        elif os.name == "nt" and os.path.isdir(link):
            # A junction reports as a dir; rmdir removes the junction, not contents.
            os.rmdir(link)
    except OSError:
        pass


def _windows_junction(link: str, real: str) -> None:  # pragma: no cover - Windows only
    import subprocess

    subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, real],
        check=True,
        capture_output=True,
    )


def list_local_files(real_path: str, *, limit: int = 2000) -> list[dict]:
    """Shallow-to-bounded recursive listing of a local project folder.

    Returns file entries ``{name, path, size, mtime}`` with ``path`` relative to
    the project root (POSIX separators on every platform), skipping
    dotfiles/VCS/dep dirs and capping at ``limit`` entries so a huge folder
    cannot stall or flood the context.
    """
    root = os.path.abspath(os.path.expanduser((real_path or "").strip()))
    if not root or not os.path.isdir(root):
        return []
    skip_dirs = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", ".idea", ".vscode"}
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                stat = os.stat(full)
                size = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                size = 0
                mtime = None
            out.append({"name": fn, "path": rel, "size": size, "mtime": mtime})
            if len(out) >= limit:
                return out
    return out


__all__ = [
    "local_link_path",
    "ensure_local_project_link",
    "remove_local_project_link",
    "list_local_files",
]
