"""Glob tool — find files by pattern, return the top 100 sorted by mtime descending.

Under the hood it uses the sandbox's find command (find is always installed in the sandbox).
Supports simple glob (``*.py``) and deep recursive glob (``**/*.py``). The two are distinguished
via find's ``-name`` / ``-path`` modes.
"""

from __future__ import annotations

import logging
from typing import Optional

from agentscope.tool import Toolkit

from core.services.project_scope import ProjectScope

from . import myspace_vfs as _ms
from ._common import resolve_sandbox_session, resp_json, sandbox_exec_bash, shell_quote
from ._paths import (
    PATH_POLICY_DOC,
    to_physical_path,
    validate_project_scope_path,
    validate_workspace_path,
)

logger = logging.getLogger(__name__)

MAX_FILES = 100


def register_glob(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def Glob(
        pattern: str,
        path: str = "/workspace",
    ) -> "ToolResponse":  # type: ignore[name-defined]
        if not pattern or not isinstance(pattern, str):
            return resp_json({"error": "pattern 必须为非空字符串"})

        path_err = validate_workspace_path(path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})

        # "My Space" → query the DB folder tree directly (faithful, cheap, does not depend on
        # whether the sandbox has been materialized); same data source as list_myspace_files /
        # Read lazy loading, fully eliminating the "list and read don't match" inconsistency.
        # Non-myspace paths still go through the sandbox find.
        if user_id and _ms.myspace_rel(path, user_id, scope) is not None:
            tree_hits = _ms.glob_tree(user_id, path, pattern, scope=scope)
            if tree_hits is not None:
                truncated = len(tree_hits) > MAX_FILES
                return resp_json({
                    "ok": True,
                    "filenames": tree_hits[:MAX_FILES],
                    "num_files": min(len(tree_hits), MAX_FILES),
                    "truncated": truncated,
                    "pattern": pattern,
                    "path": path,
                    "source": "myspace_tree",
                })

        # /myspace → /workspace/myspace/<uid>
        path = to_physical_path(path, user_id)

        # Distinguish ``**`` cross-directory matching vs plain glob:
        # - contains "**" → use find -path (needs prefix matching, strip the ``./`` prefix of **)
        # - does not → find -name (faster, no need to walk the full path)
        if "**" in pattern:
            # ``**/*.py`` → find -path "*/*.py"; ``src/**/*.py`` → -path "*/src/*/*.py"
            # simple replacement ** → * (find's -path already spans directories)
            find_pattern = pattern.replace("**", "*")
            name_flag = "-path"
            # -path needs to match the full path starting with ./xxx
            if not find_pattern.startswith("*"):
                find_pattern = "*/" + find_pattern
        else:
            find_pattern = pattern
            name_flag = "-name"

        # find -printf is available on GNU find; BusyBox find has no -printf.
        # The sandboxes (OpenSandbox + script_runner) are both based on Debian/Ubuntu → have GNU find.
        # Sort using ``%T@`` (mtime epoch) + space + ``%p`` (path).
        script = (
            f"cd {shell_quote(path)} 2>/dev/null && "
            f"find . -type f {name_flag} {shell_quote(find_pattern)} "
            f"-printf '%T@ %p\\n' 2>/dev/null "
            f"| sort -rn | head -{MAX_FILES + 1} | cut -d' ' -f2-"
        )

        exit_code, stdout, stderr = await sandbox_exec_bash(
            script, chat_id=_sess, timeout=20,
        )
        if exit_code != 0:
            return resp_json({
                "error": f"find 执行失败: {stderr or stdout or 'unknown'}",
            })

        raw_lines = [
            line.strip() for line in stdout.splitlines() if line.strip()
        ]
        # strip the ./ prefix, convert into an absolute path relative to path
        files: list[str] = []
        prefix_strip = "./"
        for ln in raw_lines:
            rel = ln[len(prefix_strip):] if ln.startswith(prefix_strip) else ln
            full = f"{path.rstrip('/')}/{rel}" if not rel.startswith("/") else rel
            files.append(full)

        truncated = len(files) > MAX_FILES
        if truncated:
            files = files[:MAX_FILES]

        return resp_json({
            "ok": True,
            "filenames": files,
            "num_files": len(files),
            "truncated": truncated,
            "pattern": pattern,
            "path": path,
        })

    Glob.__doc__ = (
        "按 glob 模式查找文件，按修改时间倒序返回前 100 条。\n\n"
        + PATH_POLICY_DOC + "\n\n"
        "本工具说明：\n"
        "- ``path`` 默认 ``/workspace``（沙盒）。仅当用户要在他「我的空间」里\n"
        "  找文件时，传 ``path='/myspace'``（或其子文件夹）——此时直接按我的\n"
        "  空间真实文件夹树匹配，结果与 ``list_myspace_files`` 一致。\n"
        "- ``pattern`` 两种风格：\n"
        "    - 普通 glob：``*.py`` / ``report_*.xlsx``（只在 ``path`` 当层匹配）\n"
        "    - 深度匹配：``**/*.py`` / ``src/**/test_*.py``（跨子目录）\n"
        "- 只返回文件（不返回目录）；最多 100 条，超出标 truncated=true。\n\n"
        "Args:\n"
        "    pattern (`str`): glob 模式。\n"
        "    path (`str`): 搜索起点，绝对路径，默认 ``/workspace``；找用户\n"
        "        「我的空间」文件时用 ``/myspace`` 或其子文件夹。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, filenames: [...], num_files, truncated,\n"
        "             pattern, path, source?}``。\n"
    )

    toolkit.register_tool_function(Glob, namesake_strategy="override")
    logger.info("[factory] Registered Glob tool (chat_id=%s)", chat_id)
