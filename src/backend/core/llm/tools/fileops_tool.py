"""Delete / Move tools — let the agent complete the CRUD loop inside the "My Space" cloud computer.

These two tools only act on ``/myspace/...`` (the user's My Space):
- ``Delete``: soft-delete a single file or an entire folder (cascading), and simultaneously clear the sandbox copy and cache.
- ``Move``: move / rename a file or folder within My Space (dst parent folder created on demand).

Non-myspace temporary files (``/workspace/scratch/...`` etc.) are not managed by these two tools ——
those are one-off sandbox products; just use ``bash``'s ``rm`` / ``mv``.
"""

from __future__ import annotations

import logging
from typing import Optional

from agentscope.tool import Toolkit

from core.services.project_scope import ProjectScope

from . import myspace_vfs as _ms
from ._common import (
    myspace_write_guard,
    resolve_sandbox_session,
    resp_json,
    sandbox_exec_bash,
    shell_quote,
)
from ._myspace_confirm import OP_DELETE, OP_MKDIR, OP_MOVE
from ._paths import (
    PATH_POLICY_DOC,
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

    async def Delete(path: str) -> "ToolResponse":  # type: ignore[name-defined]
        if not user_id:
            return resp_json({"error": "缺少 user_id，无法操作我的空间"})
        path_err = validate_workspace_path(path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})
        rel = _ms.myspace_rel(path, user_id, scope)
        if rel is None:
            return resp_json({"error": (
                "Delete 仅作用于「我的空间」(/myspace/...)。临时文件请用 "
                "bash 的 rm。"
            )})
        if rel == "":
            return resp_json({"error": "不允许删除我的空间根目录"})

        _g = await myspace_write_guard(
            chat_id=chat_id, op=OP_DELETE, logical_path=path,
            is_myspace=True, interactive=interactive,
            summary=f"删除 {path}（软删，可恢复但需用户知情）",
        )
        if _g is not None:
            return _g

        result = _ms.sync_delete(user_id, path, scope=scope)
        if "error" in result:
            return resp_json(result)

        # Simultaneously clear the sandbox physical copy (file or directory) + invalidate Read state
        physical = to_physical_path(path, user_id)
        try:
            await sandbox_exec_bash(
                f"rm -rf {shell_quote(physical)}", chat_id=_sess, timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[delete] 清沙盒副本失败 %s: %s", physical, exc)
        state.forget(path)
        state.forget(physical)
        return resp_json(result)

    Delete.__doc__ = (
        "删除用户「我的空间」里的文件或文件夹（软删，可恢复）。\n\n"
        + PATH_POLICY_DOC + "\n\n"
        "本工具说明：\n"
        "- **仅在用户明确要求删除/清理他「我的空间」里的东西时才调用。**\n"
        "  不要主动用它去删用户的文件。沙盒里的临时产物用 bash 的 rm，不归这。\n"
        "- ``path`` 必须是 ``/myspace/<文件夹>/<文件名>`` 或 ``/myspace/<文件夹>``。\n"
        "- 优先按**文件**解析；匹配不到再按**文件夹**解析（删文件夹会级联软删\n"
        "  其下全部子文件夹与文件，返回 ``artifacts_affected``）。\n"
        "- 不允许删根 ``/myspace``。\n\n"
        "Args:\n"
        "    path (`str`): 我的空间内的文件或文件夹路径。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, kind: 'file'|'folder', removed,\n"
        "             artifacts_affected?}`` 或 ``{error: '...'}``。\n"
    )

    toolkit.register_tool_function(Delete, namesake_strategy="override")
    logger.info("[factory] Registered Delete tool (chat_id=%s)", chat_id)


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

    async def Move(
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
            return resp_json({"error": (
                "Move 的源和目标都必须在「我的空间」(/myspace/...) 内。"
            )})

        _g = await myspace_write_guard(
            chat_id=chat_id, op=OP_MOVE, logical_path=src_path,
            is_myspace=True, interactive=interactive,
            summary=f"移动/改名 {src_path} → {dst_path}",
        )
        if _g is not None:
            return _g

        result = _ms.sync_move(user_id, src_path, dst_path, scope=scope)
        if "error" in result:
            return resp_json(result)

        # Move it on the sandbox side too, to keep the same-session view consistent; failure is non-blocking (lazy loading self-heals)
        src_phys = to_physical_path(src_path, user_id)
        dst_phys = to_physical_path(dst_path, user_id)
        try:
            parent = dst_phys.rsplit("/", 1)[0]
            await sandbox_exec_bash(
                f"mkdir -p {shell_quote(parent)} && "
                f"mv {shell_quote(src_phys)} {shell_quote(dst_phys)} 2>/dev/null || true",
                chat_id=_sess, timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[move] 沙盒侧 mv 失败 %s→%s: %s", src_phys, dst_phys, exc)
        state.forget(src_path)
        state.forget(src_phys)
        return resp_json(result)

    Move.__doc__ = (
        "在用户「我的空间」内移动 / 改名文件或文件夹。\n\n"
        + PATH_POLICY_DOC + "\n\n"
        "本工具说明：\n"
        "- **仅在用户明确要求移动/改名/整理他「我的空间」里的东西时才调用。**\n"
        "  沙盒里临时文件的移动用 bash 的 mv，不归这。\n"
        "- ``src_path`` / ``dst_path`` 都必须是 ``/myspace/...``。\n"
        "- 文件：改名 + 换文件夹（dst 路径里不存在的文件夹自动创建）；file_id\n"
        "  与下载链接保持不变。目标已存在同名文件会被拒绝（不静默覆盖）。\n"
        "- 文件夹：移动 / 改名整棵子树。\n\n"
        "Args:\n"
        "    src_path (`str`): 源文件或文件夹路径。\n"
        "    dst_path (`str`): 目标路径（含新名字）。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, kind: 'file'|'folder', src, dst}`` 或\n"
        "    ``{error: '...'}``。\n"
    )

    toolkit.register_tool_function(Move, namesake_strategy="override")
    logger.info("[factory] Registered Move tool (chat_id=%s)", chat_id)


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

    async def CreateFolder(path: str) -> "ToolResponse":  # type: ignore[name-defined]
        if not user_id:
            return resp_json({"error": "缺少 user_id，无法操作我的空间"})
        err = validate_workspace_path(path)
        if err:
            return resp_json({"error": err})
        scope_err = validate_project_scope_path(path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})
        if not _is_myspace_logical(path, user_id, scope):
            return resp_json({"error": (
                "CreateFolder 只能在「我的空间」(/myspace/...) 内建文件夹。"
                "沙盒里建临时目录用 bash 的 mkdir。"
            )})

        _g = await myspace_write_guard(
            chat_id=chat_id, op=OP_MKDIR, logical_path=path,
            is_myspace=True, interactive=interactive,
            summary=f"创建文件夹 {path}",
        )
        if _g is not None:
            return _g

        result = _ms.sync_mkdir(user_id, path, scope=scope)
        if "error" in result:
            return resp_json(result)

        # Create it on the sandbox side too, to keep the same-session view consistent; failure is non-blocking (lazy loading self-heals)
        try:
            phys = to_physical_path(path, user_id)
            await sandbox_exec_bash(
                f"mkdir -p {shell_quote(phys)} 2>/dev/null || true",
                chat_id=_sess, timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mkdir] 沙盒侧 mkdir 失败 %s: %s", path, exc)
        return resp_json(result)

    CreateFolder.__doc__ = (
        "在用户「我的空间」内创建文件夹（含路径上缺失的各级父文件夹，幂等）。\n\n"
        + PATH_POLICY_DOC + "\n\n"
        "本工具说明：\n"
        "- **仅在用户明确要在他「我的空间」里建文件夹/搭目录结构时才调用。**\n"
        "  沙盒里建临时目录用 bash 的 ``mkdir``，不归这。\n"
        "- 通常**不需要**先建文件夹再放文件——直接 Write/Move 到嵌套路径，\n"
        "  路径上缺的文件夹会自动创建。仅当用户要的就是一个**空文件夹**、\n"
        "  或需先把目录结构搭好时才用本工具。\n"
        "- ``path`` 必须是 ``/myspace/...``；多级路径会逐级建出。\n"
        "- 幂等：文件夹已存在不报错（返回 ``created: false``）。\n\n"
        "Args:\n"
        "    path (`str`): 要创建的文件夹路径，如 ``/myspace/报告/2026``。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, kind: 'folder', path, created}`` 或\n"
        "    ``{error: '...'}``。``created=false`` 表示本就存在。\n"
    )

    toolkit.register_tool_function(CreateFolder, namesake_strategy="override")
    logger.info("[factory] Registered CreateFolder tool (chat_id=%s)", chat_id)
