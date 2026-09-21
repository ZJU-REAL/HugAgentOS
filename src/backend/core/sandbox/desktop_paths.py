"""Desktop filesystem rules. Absolute paths are identities, never aliases."""

import ntpath
import os

from services.script_runner_service.workspace_paths import session_root


def workspace_directory(root: str, session_id: str | None) -> str:
    return session_root(root, session_id) if session_id else root


def validate_path(path: str, root: str) -> str | None:
    if not isinstance(path, str) or not path:
        return "path 必须为非空字符串"
    if path == "/myspace" or path.startswith("/myspace/"):
        return "本机模式下没有「我的空间」（/myspace/）。"
    # Native tools apply the permission gate to the resolved real path.
    return None


def resolve_path(path: str, root: str, session_id: str | None, user_id=None) -> str:
    path = os.path.expanduser(path)
    if not os.path.isabs(path) and not ntpath.isabs(path):
        path = os.path.join(workspace_directory(root, session_id), path)
    return os.path.realpath(os.path.abspath(path))


def skill_directory(skill_id: str, real_dir: str) -> str:
    return os.path.realpath(real_dir)


def quote_shell_path(path: str) -> str:
    import shlex

    # Git Bash accepts drive-letter paths with forward slashes.
    if ntpath.splitdrive(path)[0]:
        path = path.replace(chr(92), "/")
    return shlex.quote(path)


def resolve_skill_file(file_path, loader=None):
    return None


def validate_project_scope_path(path, project_folder_name):
    return None


def is_myspace_physical(path, user_id, root):
    return False


def bash_workspace_instructions(root: str, session_id: str | None) -> str:
    cwd = workspace_directory(root, session_id)
    from prompts.desktop_templates import render_desktop_part
    return render_desktop_part("bash_tool", cwd=cwd)
