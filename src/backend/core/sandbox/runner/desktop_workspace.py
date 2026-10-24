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

    from . import server, user_env

    clean = {"PY_BIN": sys.executable}
    if not user_env.is_user_folder(cwd, server.WORKSPACE_ROOT):
        return clean
    # A folder the user chose works like their own terminal; the runner's
    # scratch sessions keep the clean environment. Built-in skills are linked
    # under the workspace; plugin skills stay in the capability store.
    # The runner ships without backend modules, so the env name is spelled out here.
    skill_roots = [os.getenv("SANDBOX_SKILLS_DIR", "").strip(), os.getenv("HUGAGENT_CAPS_ROOT", "").strip()]
    try:
        return user_env.project_environment({**server.SAFE_ENV, **clean}, server.WORKSPACE_ROOT, skill_roots)
    except OSError:
        # Never let the environment stop a command: run it as before instead.
        return clean


def execution_directory(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(400, "cwd must be an absolute local directory")
    candidate = candidate.resolve()
    if not candidate.is_dir() or not os.access(candidate, os.R_OK | os.X_OK):
        raise HTTPException(400, f"cwd does not exist or is inaccessible: {candidate}")
    return candidate
