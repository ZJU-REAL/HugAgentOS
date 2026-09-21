"""Container sandbox paths and My Space aliases; never used by desktop tools."""

import ntpath
import os
import posixpath
from typing import Any, Optional

from core.agent_skills.loader import get_skill_loader


def _is_traversal_or_bad(path: str) -> Optional[str]:
    """Reject traversal/double-slash early. Returns error message or None."""
    if not path or not isinstance(path, str):
        return "path 必须为非空字符串"
    if "/../" in path or path.endswith("/..") or "//" in path:
        return f"path 不允许包含 .. 或 //: {path}"
    return None


def validate_path(path: str, root: str) -> str | None:
    error = _is_traversal_or_bad(path)
    if error:
        return error
    if ".." in path.replace(chr(92), "/").split("/"):
        return f"path 不允许包含 ..: {path}"
    if path == "/myspace" or path.startswith("/myspace/"):
        return None
    if path == root or path.startswith(root + "/"):
        return None
    if not posixpath.isabs(path) and not ntpath.isabs(path):
        return None
    return f"path 必须在 {root}/ 或 /myspace/ 下，或使用工作目录相对路径: {path}"


def workspace_directory(root: str, session_id: str | None) -> str:
    from core.config.settings import settings
    from services.script_runner_service.workspace_paths import session_root

    if settings.sandbox.provider == "script_runner" and session_id:
        return session_root(root, session_id)
    return root


def resolve_path(path: str, root: str, session_id: str | None, user_id=None) -> str:
    if path == "/myspace" or path.startswith("/myspace/"):
        return root + "/myspace/" + user_id + path[len("/myspace") :] if user_id else path
    if not posixpath.isabs(path):
        return posixpath.join(workspace_directory(root, session_id), path)
    from core.config.settings import settings

    if settings.sandbox.provider == "script_runner" and session_id:
        if path == root:
            return workspace_directory(root, session_id)
        if path.startswith(root + "/") and not path.startswith(
            (
                root + "/.sessions/",
                root + "/myspace/",
            )
        ):
            return posixpath.join(workspace_directory(root, session_id), path[len(root) + 1 :])
    return path


def skill_directory(skill_id: str, real_dir: str) -> str:
    return f"/workspace/skills/{skill_id}"


def quote_shell_path(path: str) -> str:
    import shlex

    return shlex.quote(path)


def resolve_skill_file(file_path: str, loader: Any = None) -> str | None:
    """Try to resolve a non-existent skill file path to the materialized cache."""
    parts = file_path.replace("\\", "/").split("/")
    candidates: list[tuple[str, str]] = []
    for i, seg in enumerate(parts):
        if seg == "skills" and i + 2 <= len(parts) - 1:
            skill_id = parts[i + 1]
            rel_path = "/".join(parts[i + 2 :])
            if skill_id and rel_path:
                candidates.append((skill_id, rel_path))

    if getattr(loader, "capability_run", None) is not None:
        for skill_id, rel_path in reversed(candidates):
            skill_dir = loader.get_skill_dir(skill_id)
            if skill_dir:
                candidate = os.path.join(skill_dir, rel_path)
                if os.path.exists(candidate):
                    return candidate
        return None

    from core.agent_skills.config import get_sandbox_skills_dir

    cache_root = str(get_sandbox_skills_dir())
    for skill_id, rel_path in reversed(candidates):
        cache_path = os.path.join(cache_root, skill_id, rel_path)
        if os.path.exists(cache_path):
            return cache_path

        try:
            loader = get_skill_loader()
            skill_dir = loader.get_skill_dir(skill_id)
            if skill_dir:
                candidate = os.path.join(skill_dir, rel_path)
                if os.path.exists(candidate):
                    return candidate
        except Exception:
            pass

    return None


def validate_project_scope_path(path: str, project_folder_name: Optional[str]) -> Optional[str]:
    """Extra constraint in project mode: ``/myspace/`` paths must fall under the hooked folder.

    - ``project_folder_name`` is ``None`` / empty → not project mode, no check;
    - ``/workspace/`` paths are the sandbox temp area, exempt from the project sandbox constraint;
    - ``/myspace/<x>/...`` requires ``<x>`` to equal ``project_folder_name``.
    """
    if not project_folder_name:
        return None
    if not path or not isinstance(path, str):
        return None  # validate_workspace_path already rejected
    if not (path == "/myspace" or path.startswith("/myspace" + "/")):
        return None
    rest = path[len("/myspace") :].lstrip("/")
    first = rest.split("/", 1)[0] if rest else ""
    if first == project_folder_name:
        return None
    return (
        f"项目模式下文件工具只能操作 /myspace/{project_folder_name}/ 下的文件，"
        f"不能访问 /myspace/{first or '(根)'}/。"
    )


def is_myspace_physical(physical_path: str, user_id: Optional[str], root: str) -> bool:
    """Is this physical path inside the current user's myspace persistent area?

    Both ``/workspace/myspace/{user_id}/foo`` and the logical ``/myspace/foo``
    (after translation) end up here.
    """
    if not user_id:
        return False
    prefix = f"{root}/myspace/{user_id}/"
    return physical_path == prefix.rstrip("/") or physical_path.startswith(prefix)


def bash_workspace_instructions(root: str, session_id: str | None) -> str:
    _WS = root
    return (
        "在沙盒里执行一条 shell 命令（默认 bash 解释器）。\n\n"
        "约定：\n"
        f"- 工作目录默认 {_WS}。已加载的技能文件位于 {_WS}/skills/<skill_id>/，\n"
        f'  典型用法：bash(command="cd {_WS}/skills/<id> && bash scripts/foo.sh")。\n'
        "- 用户「我的空间」在沙盒里就挂在 /myspace/ 下，写进去的文件会自动同步回\n"
        "  「我的空间」，不必再登记。路径只有 /myspace/... 这一种写法。\n"
        f"- 多步骤工作流可以连用多次 bash——{_WS} 在整轮对话内是持久的，\n"
        "  上一条命令写下的文件下一条命令直接能读。\n"
        "- 用户上传的文件不会自动出现在沙盒里。需要时先调 \n"
        f"  sandbox_put_artifact(artifact_id, dest_path) 把它拷进 {_WS}。\n"
        "- 脚本产出的文件如需让用户下载，调用 sandbox_get_artifact(src_path) 把它\n"
        "  登记成 artifact——bash 本身不会自动登记产物。\n\n"
    )
