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
    return (
        "在本机执行 Bash 命令。\n\n"
        f"当前会话默认工作目录：{cwd}。相对路径以此为准，跨轮保持不变。\n"
        "绑定本机项目时以该项目的真实目录为准。查询实际目录可执行 pwd。\n"
        "技能文件使用技能列表提供的真实路径；输入输出可使用工作目录相对路径。\n"
        "本机文件不会通过 /myspace 自动同步；需要下载链接时使用 sandbox_get_artifact。\n\n"
    )
