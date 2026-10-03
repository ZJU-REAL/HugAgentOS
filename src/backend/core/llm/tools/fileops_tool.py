"""space_delete / space_move tools — let the agent complete the CRUD loop inside the "My Space" cloud computer.

space_delete supports personal and authorized team spaces; space_move stays personal:
- ``space_delete``: soft-delete a single file or an entire folder (cascading), and simultaneously clear the sandbox copy and cache.
- ``space_move``: move / rename a file or folder within My Space (dst parent folder created on demand).

Non-myspace temporary files (``/workspace/scratch/...`` etc.) are not managed by these two tools ——
those are one-off sandbox products; just use ``Bash``'s ``rm`` / ``mv``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from agentscope.tool import Toolkit

from core.services.project_scope import ProjectScope

from core.llm.tools import myspace_vfs as _ms
from ._common import (
    resolve_sandbox_session,
    resp_json,
    sandbox_exec_bash,
    shell_quote,
)
from ._paths import (
    to_physical_path,
    validate_project_scope_path,
    validate_workspace_path,
)
from ._state import ReadStateTracker

logger = logging.getLogger(__name__)


def _is_myspace_logical(
    path: str,
    user_id: Optional[str],
    scope: Optional[ProjectScope] = None,
) -> bool:
    return user_id is not None and _ms.myspace_rel(path, user_id, scope) is not None


def register_delete(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    state: ReadStateTracker,
    interactive: bool = True,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def space_delete(path: str) -> "ToolResponse":  # type: ignore[name-defined]
        if not user_id:
            return resp_json({"error": "缺少 user_id，无法操作空间文件"})
        path_err = validate_workspace_path(path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})
        from core.services.edition_workspace import is_organization_path, delete_organization_path

        physical = to_physical_path(path, user_id, session_id=_sess, scope=scope)
        if is_organization_path(scope, user_id, path):
            from fastapi import HTTPException

            try:
                result = await asyncio.to_thread(delete_organization_path, scope, user_id, physical)
            except HTTPException as exc:
                return resp_json({"error": exc.detail, "status": exc.status_code})
            state.forget(path)
            state.forget(physical)
            return resp_json(result)
        rel = _ms.myspace_rel(path, user_id, scope)
        if rel is None:
            return resp_json(
                {
                    "error": (
                        "space_delete 支持个人空间及已授权团队空间。临时文件请用 " "Bash 的 rm。"
                    )
                }
            )
        if rel == "":
            return resp_json({"error": "不允许删除我的空间根目录"})

        result = await asyncio.to_thread(_ms.sync_delete, user_id, path, scope=scope)
        if "error" in result:
            return resp_json(result)

        # Simultaneously clear the sandbox physical copy (file or directory) + invalidate Read state
        physical = to_physical_path(path, user_id, session_id=_sess, scope=scope)
        try:
            await sandbox_exec_bash(
                f"rm -rf {shell_quote(physical)}",
                chat_id=_sess,
                user_id=user_id,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[delete] 清沙盒副本失败 %s: %s", physical, exc)
        state.forget(path)
        state.forget(physical)
        return resp_json(result)

    space_delete.__doc__ = (
        "删除个人或已授权团队空间的文件、文件夹。\n\n"
        "- ``path`` 为 ``/myspace/...`` 内的路径；\n"
        "  不允许删除个人或团队空间根目录。团队管理员可删任意文件，编辑者仅能删自己的文件，删除文件夹时全部活跃文件须属于本人；只读成员不可删。\n"
        "- 优先按**文件**解析；匹配不到再按**文件夹**解析（级联删除其下全部内容，\n"
        "  返回 ``artifacts_affected``）。\n\n"
        "Args:\n"
        "    path (`str`): 个人或团队空间内的文件或文件夹路径。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, kind: 'file'|'folder', removed,\n"
        "             artifacts_affected?}`` 或 ``{error: '...'}``。\n"
    )

    toolkit.register_tool_function(space_delete, namesake_strategy="override")
    logger.info("[factory] Registered space_delete tool (chat_id=%s)", chat_id)


def register_move(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    state: ReadStateTracker,
    interactive: bool = True,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def space_move(
        src_path: str,
        dst_path: str,
    ) -> "ToolResponse":  # type: ignore[name-defined]
        if not user_id:
            return resp_json({"error": "缺少 user_id，无法操作我的空间"})
        for p in (src_path, dst_path):
            err = validate_workspace_path(p)
            if err:
                return resp_json({"error": err})
            scope_err = validate_project_scope_path(p, project_folder_name)
            if scope_err:
                return resp_json({"error": scope_err})
        if not _is_myspace_logical(src_path, user_id, scope) or not _is_myspace_logical(
            dst_path, user_id, scope
        ):
            return resp_json(
                {"error": ("space_move 的源和目标都必须在「我的空间」(/myspace/...) 内。")}
            )

        result = await asyncio.to_thread(_ms.sync_move, user_id, src_path, dst_path, scope=scope)
        if "error" in result:
            return resp_json(result)

        # space_move it on the sandbox side too, to keep the same-session view consistent; failure is non-blocking (lazy loading self-heals)
        src_phys = to_physical_path(src_path, user_id, session_id=_sess, scope=scope)
        dst_phys = to_physical_path(dst_path, user_id, session_id=_sess, scope=scope)
        try:
            parent = dst_phys.rsplit("/", 1)[0]
            await sandbox_exec_bash(
                f"mkdir -p {shell_quote(parent)} && "
                f"mv {shell_quote(src_phys)} {shell_quote(dst_phys)} 2>/dev/null || true",
                chat_id=_sess,
                user_id=user_id,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[move] 沙盒侧 mv 失败 %s→%s: %s", src_phys, dst_phys, exc)
        state.forget(src_path)
        state.forget(src_phys)
        return resp_json(result)

    space_move.__doc__ = (
        "在用户「我的空间」内移动 / 改名文件或文件夹（``src_path`` / ``dst_path``\n"
        "都必须是 ``/myspace/...``）。\n\n"
        "- 文件：改名 + 换文件夹（dst 路径里不存在的文件夹自动创建）；file_id 与\n"
        "  下载链接保持不变。目标已存在同名文件会被拒绝（不静默覆盖）。\n"
        "- 文件夹：移动 / 改名整棵子树。\n\n"
        "Args:\n"
        "    src_path (`str`): 源文件或文件夹路径。\n"
        "    dst_path (`str`): 目标路径（含新名字）。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, kind: 'file'|'folder', src, dst}`` 或\n"
        "    ``{error: '...'}``。\n"
    )

    toolkit.register_tool_function(space_move, namesake_strategy="override")
    logger.info("[factory] Registered space_move tool (chat_id=%s)", chat_id)


def register_mkdir(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    interactive: bool = True,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def space_create_folder(path: str) -> "ToolResponse":  # type: ignore[name-defined]
        if not user_id:
            return resp_json({"error": "缺少 user_id，无法操作我的空间"})
        err = validate_workspace_path(path)
        if err:
            return resp_json({"error": err})
        scope_err = validate_project_scope_path(path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})
        if not _is_myspace_logical(path, user_id, scope):
            return resp_json(
                {
                    "error": (
                        "space_create_folder 只能在「我的空间」(/myspace/...) 内建文件夹。"
                        "沙盒里建临时目录用 Bash 的 mkdir。"
                    )
                }
            )

        result = await asyncio.to_thread(_ms.sync_mkdir, user_id, path, scope=scope)
        if "error" in result:
            return resp_json(result)

        # Create it on the sandbox side too, to keep the same-session view consistent; failure is non-blocking (lazy loading self-heals)
        try:
            phys = to_physical_path(path, user_id, session_id=_sess, scope=scope)
            await sandbox_exec_bash(
                f"mkdir -p {shell_quote(phys)} 2>/dev/null || true",
                chat_id=_sess,
                user_id=user_id,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mkdir] 沙盒侧 mkdir 失败 %s: %s", path, exc)
        return resp_json(result)

    space_create_folder.__doc__ = (
        "在用户「我的空间」内创建文件夹（含路径上缺失的各级父文件夹，幂等；\n"
        "``path`` 必须是 ``/myspace/...``）。\n\n"
        "通常**不需要**先建文件夹再放文件——直接 Write/space_move 到嵌套路径，缺的\n"
        "文件夹会自动创建；仅当用户要的就是一个**空文件夹**、或需先把目录结构\n"
        "搭好时才用本工具。已存在不报错（返回 ``created: false``）。\n\n"
        "Args:\n"
        "    path (`str`): 要创建的文件夹路径，如 ``/myspace/报告/2026``。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, kind: 'folder', path, created}`` 或\n"
        "    ``{error: '...'}``。``created=false`` 表示本就存在。\n"
    )

    toolkit.register_tool_function(space_create_folder, namesake_strategy="override")
    logger.info("[factory] Registered space_create_folder tool (chat_id=%s)", chat_id)
