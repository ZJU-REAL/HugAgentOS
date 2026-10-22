"""Container-only skill links and logical path handling for the shared runner."""

import os
import re
import shlex
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

WORKSPACE_ROOT = os.getenv("SCRIPT_RUNNER_WORKSPACE", "/workspace")
SHARED_SKILLS_DIR = "sandbox_skills"
USER_SKILLS_DIR = ".skills_u"


def _validate_user_id(user_id):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", user_id or ""):
        raise HTTPException(400, "invalid user_id")


_WS_PATH_RE = re.compile(
    r'(?<![A-Za-z0-9_.\\/-])/workspace(?!/(?:\.sessions|myspace)(?:/|$))(?=/|$|["\'\s:;)&|])'
)

# 模型只认识 /myspace 这一种写法。opensandbox / cube 一人一沙箱，能在容器里建软链；
# 这个 runner 是所有用户共用一个服务，根上建全局软链会指向"最后一个用的人"，属于跨用户
# 串数据。所以改成按请求里的 user_id 就地改写路径，根目录映射仍交给既有的 /workspace 链路。
_MYSPACE_PATH_RE = re.compile(r'(?<![A-Za-z0-9_.\\/-])/myspace(?=/|$|["\'\s:;)&|])')


def _rewrite_myspace_refs(value: str, user_id: Optional[str]) -> str:
    """把 /myspace[/...] 展开成 /workspace/myspace/{uid}[/...]。

    没有 user_id 时原样返回 —— 无从判断是谁的空间，宁可让路径不存在而报错，
    也不能猜一个用户。
    """
    if not isinstance(value, str) or not user_id:
        return value
    _validate_user_id(user_id)
    return _MYSPACE_PATH_RE.sub(f"/workspace/myspace/{user_id}", value)


def _bash_quote_state(value: str, end: int) -> Optional[str]:
    """Return the shell quote containing a container path prefix."""
    state: Optional[str] = None
    escaped = False
    for char in value[:end]:
        if state == "single":
            if char == "'":
                state = None
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif state == "double":
            if char == '"':
                state = None
        elif char == "'":
            state = "single"
        elif char == '"':
            state = "double"
    return state


def _quote_bash_path_refs(
    value: str,
    pattern: re.Pattern[str],
    replacement: str,
) -> str:
    """Replace path prefixes while preserving or adding shell-safe quoting."""

    def _replace(match: re.Match[str]) -> str:
        quote_state = _bash_quote_state(value, match.start())
        if quote_state == "single":
            return replacement.replace("'", "'\"'\"'")
        if quote_state == "double":
            return (
                replacement.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("$", "\\$")
                .replace("`", "\\`")
            )
        # Quoting only the rewritten prefix is valid shell concatenation:
        # '/workspace/.sessions/<id>'/site resolves as one path while the suffix
        # remains visible to the boundary-matching regex.
        return shlex.quote(replacement)

    return pattern.sub(_replace, value)


def _ensure_shared_dir_link(link: Path, target: Path) -> None:
    """Expose the container's read-only mount without backend dependencies."""
    if not target.exists():
        return
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return
        link.unlink()
    elif link.exists():
        raise HTTPException(409, f"技能链接位置已被文件或目录占用：{link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target.resolve(), target_is_directory=True)
    except FileExistsError:
        if not link.is_symlink() or link.resolve() != target.resolve():
            raise HTTPException(409, f"技能链接位置冲突：{link}")


def _user_skill_views_root(root: str) -> Path:
    """Locate container user views from the configured mount or default mount."""
    skills_root = os.getenv("SANDBOX_SKILLS_DIR", "").strip()
    if skills_root:
        root = Path(skills_root)
        return root.parent / f"{root.name}_u"
    return Path(root) / USER_SKILLS_DIR


def _skills_dir_for(root: str, user_id: Optional[str]) -> Path:
    """The skill tree one session may see: the user's own view, else shared-only.

    The user view holds that user's private skills plus a link per shared skill,
    so another user's private skill files (a market skill's secrets.json among
    them) are never reachable from this session. Falls back to the shared tree
    when the user has no view yet, and to the legacy single mount when a
    deployment has not picked up the two skill mounts yet.
    """
    shared = Path(root) / SHARED_SKILLS_DIR
    if user_id:
        view = _user_skill_views_root(root) / user_id
        if view.is_dir():
            return view
    if shared.is_dir():
        return shared
    skills_root = os.getenv("SANDBOX_SKILLS_DIR", "").strip()
    if skills_root and Path(skills_root).is_dir():
        return Path(skills_root)
    return Path(root) / "skills"


def prepare_workspace(workspace: Path, root: str, user_id, capability_view_key=None):
    _ensure_shared_dir_link(workspace / "skills", _skills_dir_for(root, user_id))
    if user_id:
        shared = Path(root) / "myspace" / user_id
        shared.mkdir(parents=True, exist_ok=True)
        _ensure_shared_dir_link(workspace / "myspace" / user_id, shared)


def execution_text(text, language, workspace, user_id):
    text = _rewrite_myspace_refs(text, user_id)
    if language == "bash":
        return _quote_bash_path_refs(text, _WS_PATH_RE, str(workspace))
    return _WS_PATH_RE.sub(lambda match: str(workspace), text)


def file_path(path: str, workspace: Path, root: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        if path.startswith("/workspace/") and not path.startswith(
            (
                "/workspace/.sessions/",
                "/workspace/myspace/",
            )
        ):
            return workspace / path[len("/workspace/") :]
        if path == "/workspace":
            return workspace
        return candidate
    return workspace / candidate


def extra_roots(root: str, user_id, *, read_only=False):
    roots = [Path(root) / "myspace" / user_id] if user_id else []
    if read_only:
        roots.append(_skills_dir_for(root, user_id))
        # Shared entries in a user view resolve outside that view. These mounts
        # contain shared skills only; never grant other users' private roots.
        roots.append(Path(root) / SHARED_SKILLS_DIR)
        shared_mount = os.getenv("SANDBOX_SKILLS_DIR", "").strip()
        if shared_mount:
            roots.append(Path(shared_mount))
    return roots


def subprocess_environment(cwd):
    return {"PDF_SKILL_DIR": str(Path(cwd) / "skills" / "pdf-editing")}
