"""Exact-text replacement edits of files in the active sandbox."""

from __future__ import annotations
from core.llm.factory.tools.definitions import function_tool

import difflib
import logging
from typing import Optional

from agentscope.tool import Toolkit, ToolResponse
from core.projects.edition import ProjectScope

from ._common import (
    resolve_sandbox_session,
    resp_json,
)
from core.sandbox.workspace.resolution import space_metadata, to_physical_path, validate_project_scope_path, validate_workspace_path
from ._mutation_queue import file_mutation_lock
from .text_match import EditMatchError, apply_edit

logger = logging.getLogger(__name__)


def _make_unified_diff(
    path: str,
    old_content: str,
    new_content: str,
    n_context: int = 3,
) -> str:
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=False),
        new_content.splitlines(keepends=False),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=n_context,
        lineterm="",
    )
    return "\n".join(diff)


def register_edit(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def Edit(
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> "ToolResponse":  # type: ignore[name-defined]
        from core.sandbox.permissions.dispatch_models import PermissionEnforcementError, require_local_path_permission
        from core.sandbox import SandboxConnectError as _SCE
        from core.sandbox import SandboxError as _SE
        from core.sandbox import get_sandbox_provider as _get_provider

        # ── Basic input validation ───────────────────────────────────────
        path_err = validate_workspace_path(file_path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(file_path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})

        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return resp_json({"error": "old_string / new_string 必须是字符串"})

        if old_string == new_string:
            return resp_json(
                {
                    "error": "old_string 与 new_string 相同——这次 Edit 不会改变文件内容",
                }
            )

        from .project_source_access import current_scope_error

        scope_error = current_scope_error(scope, user_id, write=True)
        if scope_error:
            return resp_json(scope_error)
        physical = to_physical_path(file_path, user_id, session_id=_sess, scope=scope)


        # No read-before-edit precondition (aligned with pi-mono): the model is trusted to
        # know what it edits; old_string matching + uniqueness is the only guard.
        provider = _get_provider()
        from core.config.local_mode import local_mode_enabled as _local_on

        _is_local = _local_on()
        async with file_mutation_lock(physical):
            try:
                if _is_local:
                    # Local desktop mode: read the real host file directly (works for
                    # real paths outside the sandbox workspace, which put/get refuse).
                    require_local_path_permission(physical, "read")
                    with open(physical, "rb") as _f:
                        current_bytes = _f.read()
                else:
                    current_bytes = await provider.get_file(_sess, physical, user_id=user_id)
            except PermissionEnforcementError as exc:
                return resp_json({"error": str(exc), "blocked": True})
            except (_SE, _SCE, OSError) as exc:
                return resp_json({"error": f"读取文件失败: {exc}"})

            try:
                current_raw = current_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return resp_json({"error": "文件不是合法 UTF-8 文本，无法 Edit"})
            try:
                result = apply_edit(current_raw, old_string, new_string, replace_all)
            except EditMatchError as exc:
                return resp_json({"error": str(exc)})
            current_text, new_text, replaced = result.old_text, result.new_text, result.replaced

            # ── Write back ─────────────────────────────────────────────
            new_bytes = result.new_raw.encode("utf-8")
            # Local-mode write-back snapshot (ticket #08): back up before overwriting.
            try:
                from core.harness.registry.local_files import maybe_snapshot_local

                maybe_snapshot_local(physical)
            except Exception as fallback_error:
                from core.infra.failures import record_best_effort_failure
                record_best_effort_failure('core.llm.tools.edit_tool:Edit', fallback_error)
            try:
                if _is_local:
                    import os as _os

                    require_local_path_permission(physical, "write")

                    parent = _os.path.dirname(physical)
                    if parent:
                        _os.makedirs(parent, exist_ok=True)
                    with open(physical, "wb") as _f:
                        _f.write(new_bytes)
                else:
                    await provider.put_file(_sess, physical, new_bytes, user_id=user_id)
            except PermissionEnforcementError as exc:
                return resp_json({"error": str(exc), "blocked": True})
            except (_SE, _SCE, OSError) as exc:
                return resp_json({"error": f"写入失败: {exc}"})

        diff = _make_unified_diff(file_path, current_text, new_text)
        payload: dict = {
            "ok": True,
            "file_path": file_path,
            "physical_path": physical,
            "replaced": replaced,
            "replace_all": replace_all,
            "fuzzy_match": result.fuzzy,
            "diff": diff,
            "old_size": len(current_bytes),
            "new_size": len(new_bytes),
            **space_metadata(physical, user_id),
        }
        return resp_json(payload)

    Edit.__doc__ = (
        "对文本文件做精确字符串替换。改 ``/myspace/...`` 的文件同一 file_id、\n"
        "下载链接不变。\n\n"
        "- 不要求先 Read；用 Read、Bash 或任何方式看过要改的片段即可。\n"
        "- ``old_string`` 应精确匹配文件内容（不含行号前缀）。精确匹配失败时会\n"
        "  忽略行尾空白与中英文引号/破折号差异再试一次；缩进与换行仍需一致。\n"
        "- 默认 ``replace_all=false`` 时，``old_string`` 必须在文件里唯一出现。\n"
        "  扩大上下文使其唯一；或传 ``replace_all=true`` 替换所有匹配。\n"
        "- 文件的 BOM 与 CRLF/LF 换行风格会原样保留。\n\n"
        "Args:\n"
        "    file_path (`str`): 文件路径。默认相对本次会话的工作目录；仅在用户\n"
        "        要求改其「我的空间」文件时用 ``/myspace/...``。\n"
        "    old_string (`str`): 要被替换的原内容，尽量短但要在文件里唯一。\n"
        "    new_string (`str`): 替换后的新内容。必须与 old_string 不同。\n"
        "    replace_all (`bool`): 默认 false，仅替换唯一匹配；true 则替换全部。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, file_path, physical_path, replaced, replace_all,\n"
        "             fuzzy_match, diff, old_size, new_size, persistent, space_type}``\n"
        "    成功；``{error: '...'}`` 失败。\n"
    )

    toolkit.append(function_tool(Edit))
    logger.info("[factory] Registered Edit tool (chat_id=%s)", chat_id)
