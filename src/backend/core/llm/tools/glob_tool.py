"""Glob tool — find files by pattern, sorted by mtime descending, one page at a time.

Under the hood it uses the sandbox's find command (find is always installed in the sandbox).
Supports simple glob (``*.py``) and deep recursive glob (``**/*.py``). The two are distinguished
via find's ``-name`` / ``-path`` modes.
"""

from __future__ import annotations

import logging
from typing import Optional

from agentscope.tool import Toolkit
from core.db.paging import normalize_page, normalize_page_size, paging_meta
from core.services.project_scope import ProjectScope

from . import myspace_vfs as _ms
from ._common import resolve_sandbox_session, resp_json, sandbox_exec_bash, shell_quote
from ._paths import to_physical_path, validate_project_scope_path, validate_workspace_path

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
    ) -> "ToolResponse":  # type: ignore[name-defined]
        if not pattern or not isinstance(pattern, str):
            return resp_json({"error": "pattern 必须为非空字符串"})

        page_size = normalize_page_size(limit)
        page = normalize_page(page)
        offset = (page - 1) * page_size if page_size is not None else 0

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

        if scope and scope.kind == "team":
            from .project_working_copy import directory

            root = directory(scope.project_id)
            if path in (".", "/workspace") or path == root:
                path = "/myspace/" + scope.folder_name
            elif path.startswith(root + "/"):
                path = "/myspace/" + scope.folder_name + path[len(root) :]

        # "My Space" → query the DB folder tree directly (faithful, cheap, does not depend on
        # whether the sandbox has been materialized); same data source as list_myspace_files /
        # Read lazy loading, fully eliminating the "list and read don't match" inconsistency.
        # Non-myspace paths still go through the sandbox find.
        if user_id and _ms.myspace_rel(path, user_id, scope) is not None:
            tree_hits = _ms.glob_tree(user_id, path, pattern, scope=scope)
            if tree_hits is not None:
                window = tree_hits[offset : offset + page_size] if page_size else tree_hits
                meta = paging_meta(total=len(tree_hits), page=page, page_size=page_size)
                return resp_json(
                    {
                        "ok": True,
                        "filenames": window,
                        "num_files": len(window),
                        "truncated": meta["has_more"],
                        "pattern": pattern,
                        "path": path,
                        "source": "myspace_tree",
                        **meta,
                    }
                )

        # /myspace → /workspace/myspace/<uid>
        path = to_physical_path(path, user_id, session_id=_sess)

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
        # 只取到"当前页多一条"为止，多出来的那条用于判断还有没有下一页；不分页时不截断。
        head_clause = f"| head -{offset + page_size + 1} " if page_size is not None else ""
        script = (
            f"cd {shell_quote(path)} 2>/dev/null && "
            f"find . -type f {name_flag} {shell_quote(find_pattern)} "
            f"-printf '%T@ %p\\n' 2>/dev/null "
            f"| sort -rn {head_clause}| cut -d' ' -f2-"
        )

        exit_code, stdout, stderr = await sandbox_exec_bash(
            script,
            chat_id=_sess,
            user_id=user_id,
            timeout=20,
        )
        if exit_code != 0:
            return resp_json(
                {
                    "error": f"find 执行失败: {stderr or stdout or 'unknown'}",
                }
            )

        raw_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        # strip the ./ prefix, convert into an absolute path relative to path
        files: list[str] = []
        prefix_strip = "./"
        for ln in raw_lines:
            rel = ln[len(prefix_strip) :] if ln.startswith(prefix_strip) else ln
            full = f"{path.rstrip('/')}/{rel}" if not rel.startswith("/") else rel
            files.append(full)

        has_more = page_size is not None and len(files) > offset + page_size
        window = files[offset : offset + page_size] if page_size is not None else files

        return resp_json(
            {
                "ok": True,
                "filenames": window,
                "num_files": len(window),
                "truncated": has_more,
                "has_more": has_more,
                "page": page,
                "page_size": page_size,
                # 还有下一页时没往下数完，总数未知；数完了才给准数。
                "total": None if has_more else offset + len(window),
                "pattern": pattern,
                "path": path,
            }
        )

    Glob.__doc__ = (
        "按 glob 模式查找文件，按修改时间倒序分页返回。\n\n"
        "- ``path`` 默认 ``.``（本次会话的工作目录）；传 ``/myspace``（或其子文件夹）则按我的\n"
        "  空间真实目录树匹配。\n"
        "- ``pattern``：普通 glob ``*.py``（只在 ``path`` 当层匹配）、深度匹配\n"
        "  ``**/*.py`` / ``src/**/test_*.py``（跨子目录）。\n"
        "- 只返回文件（不返回目录）；结果多于一页时 ``has_more=true``，用 ``page+1``\n"
        "  继续取，没有条数上限。\n\n"
        "Args:\n"
        "    pattern (`str`): glob 模式。\n"
        "    path (`str`): 搜索起点，默认 ``.``（本次会话的工作目录）；找用户\n"
        "        「我的空间」文件时用 ``/myspace`` 或其子文件夹。\n"
        f"    limit (`int`): 每页条数，默认 {DEFAULT_GLOB_PAGE_SIZE}，无上限；\n"
        "        填 0 或负数表示不分页、一次返回全部。\n"
        "    page (`int`): 页码，从 1 开始。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, filenames: [...], num_files, page, page_size,\n"
        "             total, has_more, truncated, pattern, path, source?}``；\n"
        "    ``total`` 在还有下一页时为 null（未数完）。\n"
    )

    toolkit.register_tool_function(Glob, namesake_strategy="override")
    logger.info("[factory] Registered Glob tool (chat_id=%s)", chat_id)
