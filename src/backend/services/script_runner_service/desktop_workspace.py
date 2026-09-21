"""Desktop runner paths: use real files, with no aliases or workspace links."""

import os
import re
from pathlib import Path

from fastapi import HTTPException


def prepare_workspace(workspace: Path, root: str, user_id, capability_view_key=None):
    if capability_view_key is None:
        return
    if not re.fullmatch(r"[a-f0-9]{64}", capability_view_key):
        raise HTTPException(400, "invalid prepared capability view")
    caps = os.getenv("HUGAGENT_CAPS_ROOT", "").strip()
    if not caps:
        raise HTTPException(409, "prepared capability view requires local execution")
    caps_root = Path(caps).resolve()
    view = caps_root / ".capabilities" / "views" / capability_view_key / "skills"
    if not view.is_dir() or not view.resolve().is_relative_to(caps_root):
        raise HTTPException(409, "prepared capability view is missing or invalid")


def execution_text(text, language, workspace, user_id):
    return text


def file_path(path: str, workspace: Path, root: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else workspace / candidate


def extra_roots(root: str, user_id, *, read_only=False):
    return []


def subprocess_environment(cwd):
    import sys

    return {"PY_BIN": sys.executable}
