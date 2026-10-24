"""Authorized sandbox file search with stateless continuation."""
from __future__ import annotations
from core.llm.factory.tools.definitions import function_tool

import logging
import json
from pathlib import Path
from typing import Optional

from agentscope.tool import Toolkit, ToolResponse
from core.db.paging import normalize_page, normalize_page_size
from core.projects.edition import ProjectScope

from ._common import resolve_sandbox_session, resp_json, sandbox_exec_bash, shell_quote
from core.sandbox.workspace.resolution import to_physical_path, validate_project_scope_path, validate_workspace_path

logger = logging.getLogger(__name__)

#: 调用方没给 limit 时一页多少条。这是默认页长、不是上限：调用方可以调大、可以翻页，
#: 也可以填 0 表示一次要全部。
DEFAULT_GLOB_PAGE_SIZE = 100


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
        path: str = ".",
        limit: int = DEFAULT_GLOB_PAGE_SIZE,
        page: int = 1,
        cursor: str = "",
    ) -> "ToolResponse":  # type: ignore[name-defined]
        if not pattern or not isinstance(pattern, str):
            return resp_json({"error": "pattern 必须为非空字符串"})

        page_size = normalize_page_size(limit)
        page = normalize_page(page)
        if page > 1 and not cursor:
            return resp_json({"error": "翻页请传入上一页的 next_cursor，不支持按页码重新扫描"})

        from .project_source_access import current_scope_error

        scope_error = current_scope_error(scope, user_id)
        if scope_error:
            return resp_json(scope_error)
        path_err = validate_workspace_path(path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})

        # /myspace → /workspace/myspace/<uid>
        path = to_physical_path(path, user_id, session_id=_sess, scope=scope)

        source = Path(__file__).with_name("glob_scan.py").read_text()
        args = json.dumps({"root": path, "pattern": pattern, "limit": page_size, "cursor": cursor})
        script = "python3 -c " + shell_quote(source) + " " + shell_quote(args)

        exit_code, stdout, stderr = await sandbox_exec_bash(
            script,
            chat_id=_sess,
            user_id=user_id,
            timeout=20,
        )
        if exit_code != 0:
            return resp_json(
                {
                    "error": f"查找文件失败: {stderr or stdout or 'unknown'}",
                }
            )

        try:
            result = json.loads(stdout)
        except ValueError:
            return resp_json({"error": "查找文件返回了无效结果"})
        return resp_json(result)

    Glob.__doc__ = (
        "按路径顺序查找文件。每次调用均检查当前沙盒权限。\n"
        "path 默认会话工作目录；查找我的空间请用 /myspace 或其子目录。\n"
        "*.py 只查当前目录；**/*.py 递归查找。不会进入符号链接目录。\n"
        "结果含 next_cursor；翻页时保持 path、pattern 不变，并传入该 cursor。\n"
        "不要使用 page+1 或并行请求同一个游标。重新搜索请省略 cursor。\n"
        "遍历读取当前目录内容，不创建文件副本或结果快照；文件变动可能影响后续页。\n"
        "limit 默认 100；0 或负数返回全部。page 仅兼容第一页。"
    )

    toolkit.append(function_tool(Glob))
    logger.info("[factory] Registered Glob tool (chat_id=%s)", chat_id)
