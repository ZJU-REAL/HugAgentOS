"""Grep tool — sandbox content search. Prefers ripgrep, falls back to grep -r when absent.

Three output_modes (aligned with Claude Code):
- ``files_with_matches`` (default): returns only the list of matching file paths (-l)
- ``content``: returns ``path:lineno:line`` format (supports -A/-B context)
- ``count``: match count per file (-c)
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

DEFAULT_HEAD_LIMIT = 250
MAX_HEAD_LIMIT = 1000


_OUTPUT_MODES = {"files_with_matches", "content", "count"}


def register_grep(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def Grep(
        pattern: str,
        path: str = "/workspace",
        glob: str = "",
        output_mode: str = "files_with_matches",
        case_insensitive: bool = False,
        line_numbers: bool = True,
        context_before: int = 0,
        context_after: int = 0,
        head_limit: int = 0,
    ) -> "ToolResponse":  # type: ignore[name-defined]
        if not pattern or not isinstance(pattern, str):
            return resp_json({"error": "pattern 必须为非空字符串"})

        path_err = validate_workspace_path(path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})

        # Grep needs to search content → first materialize that subtree of "My Space" into the sandbox in bulk on demand
        # (the batch version of lazy loading; already-in-sandbox files are not re-fetched), ensuring the search covers the real My Space.
        materialized = 0
        if user_id and _ms.myspace_rel(path, user_id, scope) is not None:
            try:
                from core.sandbox import get_sandbox_provider as _gsp
                materialized = await _ms.materialize_tree(
                    _gsp(), _sess, user_id, path, scope=scope,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[grep] materialize_tree 失败 %s: %s", path, exc)

        # /myspace → /workspace/myspace/<uid>
        path = to_physical_path(path, user_id)

        if output_mode not in _OUTPUT_MODES:
            return resp_json({
                "error": (
                    f"不支持的 output_mode: {output_mode}。"
                    f"可选: {', '.join(sorted(_OUTPUT_MODES))}"
                ),
            })

        head = head_limit if head_limit > 0 else DEFAULT_HEAD_LIMIT
        head = min(head, MAX_HEAD_LIMIT)

        # Probe for rg
        probe_exit, probe_out, _ = await sandbox_exec_bash(
            "command -v rg >/dev/null 2>&1 && echo rg || echo grep",
            chat_id=_sess, timeout=5,
        )
        binary = (probe_out or "").strip() or "grep"
        if probe_exit != 0:
            binary = "grep"

        # ── Assemble command ────────────────────────────────────────────
        quoted_pattern = shell_quote(pattern)
        quoted_path = shell_quote(path)

        if binary == "rg":
            args = ["rg", "--no-heading"]
            if case_insensitive:
                args.append("-i")
            if glob:
                args += ["--glob", shell_quote(glob)]
            if output_mode == "files_with_matches":
                args.append("-l")
            elif output_mode == "count":
                args.append("-c")
            else:  # content
                if line_numbers:
                    args.append("-n")
                if context_before > 0:
                    args += ["-B", str(int(context_before))]
                if context_after > 0:
                    args += ["-A", str(int(context_after))]
            args += ["-e", quoted_pattern, quoted_path]
        else:
            # grep fallback
            args = ["grep", "-rE"]
            if case_insensitive:
                args.append("-i")
            if output_mode == "files_with_matches":
                args.append("-l")
            elif output_mode == "count":
                args.append("-c")
            else:
                if line_numbers:
                    args.append("-n")
                if context_before > 0:
                    args += ["-B", str(int(context_before))]
                if context_after > 0:
                    args += ["-A", str(int(context_after))]
            # glob uses --include pattern
            if glob:
                args += ["--include", shell_quote(glob)]
            args += ["-e", quoted_pattern, quoted_path]

        # Use head to cap output lines to prevent explosion; count mode also allows head (limits file count)
        cmd = " ".join(args) + f" 2>/dev/null | head -{head + 1}"

        exit_code, stdout, stderr = await sandbox_exec_bash(
            cmd, chat_id=_sess, timeout=30,
        )
        # grep/rg exits 1 when there are no matches, which is not an error
        if exit_code not in (0, 1):
            return resp_json({
                "error": f"搜索失败（{binary}）: {stderr or 'unknown'}",
            })

        raw_lines = [ln for ln in stdout.splitlines() if ln]
        truncated = len(raw_lines) > head
        if truncated:
            raw_lines = raw_lines[:head]

        return resp_json({
            "ok": True,
            "engine": binary,
            "pattern": pattern,
            "path": path,
            "output_mode": output_mode,
            "matches": raw_lines,
            "num_matches": len(raw_lines),
            "truncated": truncated,
            "myspace_materialized": materialized,
        })

    Grep.__doc__ = (
        "按正则搜索文件内容。优先用 ripgrep，缺失则 fallback grep。\n\n"
        + PATH_POLICY_DOC + "\n\n"
        "本工具说明：\n"
        "- ``path`` 默认 ``/workspace``（沙盒）。仅当用户要在他「我的空间」里\n"
        "  搜内容时传 ``path='/myspace'``——此时会先按需把该子树的**文本**\n"
        "  文件批量拉进沙盒再搜（返回 ``myspace_materialized`` 计数；二进制\n"
        "  文档搜不了，需要的话改用 Read 看解析文本）。\n"
        "- ``pattern`` 是正则；字面特殊符号请自行转义。\n"
        "- ``output_mode`` 三种：\n"
        "    - ``files_with_matches`` (默认) → 只返回命中文件路径\n"
        "    - ``content`` → 返回 ``path:lineno:line``，支持 -A/-B 上下文\n"
        "    - ``count`` → 每个文件的匹配次数\n"
        "- 默认 head_limit=250 行，硬上限 1000，防止上下文爆炸。\n"
        "- ``glob`` 例 ``*.py`` 可只在该类文件中搜索。\n\n"
        "Args:\n"
        "    pattern (`str`): 正则表达式。\n"
        "    path (`str`): 搜索路径，默认 ``/workspace``；搜用户「我的空间」\n"
        "        时用 ``/myspace`` 或其子文件夹。\n"
        "    glob (`str`): 可选文件名 glob 过滤（如 ``*.py``）。\n"
        "    output_mode (`str`): files_with_matches | content | count。\n"
        "    case_insensitive (`bool`): -i。\n"
        "    line_numbers (`bool`): content 模式带行号（默认 true）。\n"
        "    context_before / context_after (`int`): content 模式上下文行数。\n"
        "    head_limit (`int`): 截断行数，0 = 默认 250，硬上限 1000。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, engine, pattern, path, output_mode, matches: [...],\n"
        "             num_matches, truncated, myspace_materialized}``。\n"
    )

    toolkit.register_tool_function(Grep, namesake_strategy="override")
    logger.info("[factory] Registered Grep tool (chat_id=%s)", chat_id)
