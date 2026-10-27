"""Create or overwrite files in the active sandbox."""

from __future__ import annotations
from core.llm.factory.tools.definitions import function_tool

import difflib
import logging
from typing import Optional

from agentscope.tool import Toolkit, ToolResponse
from core.projects.edition import ProjectScope

from ._common import (
    pin_artifact_to_workspace,
    resolve_sandbox_session,
    resp_json,
    sandbox_exec_bash,
    shell_quote,
    register_file_artifact,
)
from core.sandbox.workspace.resolution import basename, is_myspace_physical, space_metadata, WORKSPACE_ROOT, parent_dir, to_physical_path, validate_project_scope_path, validate_workspace_path
from ._mutation_queue import file_mutation_lock

logger = logging.getLogger(__name__)


# Write produces UTF-8 text; saving it under these extensions is guaranteed to yield broken fake documents
_BINARY_DOC_EXTS = {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf"}


def _make_unified_diff(path: str, old: str, new: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="",
        )
    )


def register_write(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def Write(
        file_path: str,
        content: str,
        register_as_artifact: bool = False,
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

        if not isinstance(content, str):
            return resp_json({"error": "content 必须是字符串"})

        # ── Hard guard against binary document formats ───────────────────
        # Write can only produce UTF-8 text; saving text as binary formats like
        # .docx/.xlsx/.pptx/.pdf yields fake documents that cannot be opened/parsed
        # (real incident: a docx artifact was overwritten in place with markdown
        # text and the frontend failed to read it). Reject for both create and overwrite.
        _ext = basename(file_path).rsplit(".", 1)[-1].lower() if "." in basename(file_path) else ""
        if _ext in _BINARY_DOC_EXTS:
            return resp_json(
                {
                    "error": (
                        f"Write 只能写 UTF-8 纯文本，不能直接生成 .{_ext} 二进制文档"
                        "（产物会是无法打开的假文档）。请用 Bash 调命令行工具生成"
                        "（docx 用 python-docx，xlsx 用 openpyxl，pdf 用 reportlab "
                        "等），生成后写到目标目录即可。"
                    ),
                }
            )

        # ── Logical path (/myspace/...) -> physical path (/workspace/myspace/<uid>/...) ──
        from .project_source_access import current_scope_error

        scope_error = current_scope_error(scope, user_id, write=True)
        if scope_error:
            return resp_json(scope_error)
        physical = to_physical_path(file_path, user_id, session_id=_sess, scope=scope)
        is_persistent = is_myspace_physical(physical, user_id)



        from core.config.local_mode import local_mode_enabled as _local_on

        _is_local = _local_on()

        async with file_mutation_lock(physical):
            # ── Existing-file detection ─────────────────────────────────────
            provider = _get_provider()
            existing: Optional[bytes] = None
            try:
                if _is_local:
                    require_local_path_permission(physical, "read")
                    with open(physical, "rb") as _f:
                        existing = _f.read()
                else:
                    existing = await provider.get_file(_sess, physical, user_id=user_id)
            except FileNotFoundError:
                existing = None
            except PermissionEnforcementError as exc:
                return resp_json({"error": str(exc), "blocked": True})
            except _SE:
                existing = None
            except _SCE as exc:
                return resp_json({"error": f"沙盒连接失败: {exc}"})
            except OSError as exc:
                return resp_json({"error": f"读取现有文件失败: {exc}"})

            is_update = existing is not None
            original_text: Optional[str] = None

            # No read-before-overwrite precondition (aligned with pi-mono); the existing
            # content is only read to report a diff.
            if is_update:
                try:
                    original_text = existing.decode("utf-8")
                except UnicodeDecodeError:
                    original_text = None

            # ── Create parent directory (inside the sandbox) ─────────────────
            pd = parent_dir(physical)
            # The host-local script runner implements parent creation inside its
            # native put_file endpoint.  Asking it to run POSIX ``mkdir -p`` first
            # breaks standard Windows installations before the actual write starts.
            if pd and pd != WORKSPACE_ROOT and provider.name != "script_runner":
                mk_exit, _, mk_err = await sandbox_exec_bash(
                    f"mkdir -p {shell_quote(pd)}",
                    chat_id=_sess,
                    user_id=user_id,
                    timeout=10,
                )
                if mk_exit != 0:
                    return resp_json(
                        {
                            "error": f"创建父目录 {pd} 失败: {mk_err}",
                        }
                    )

            # ── Write into the sandbox ─────────────────────────────────────
            new_bytes = content.encode("utf-8")
            # Local-mode write-back snapshot (ticket #08): back up the real file
            # before overwriting it, so the user can roll back. No-op on cloud/web.
            try:
                from core.harness.registry.local_files import maybe_snapshot_local

                maybe_snapshot_local(physical)
            except Exception as fallback_error:
                from core.infra.failures import record_best_effort_failure
                record_best_effort_failure('core.llm.tools.write_tool:Write', fallback_error)
            try:
                if _is_local:
                    # Local desktop mode: the backend runs on the host, so write the
                    # real file directly. This reaches the user's actual folders
                    # (incl. real paths outside the sandbox workspace root), which the
                    # sidecar put_file refuses.
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
            except (_SE, _SCE) as exc:
                return resp_json({"error": f"写入失败: {exc}"})
            except OSError as exc:
                return resp_json({"error": f"写入失败: {exc}"})

        artifact_ref: Optional[dict] = None
        needs_registration = bool(register_as_artifact and user_id and not is_persistent)
        if needs_registration and _is_local:
            from core.files.artifacts.local_reference import reference_local_file
            from fastapi import HTTPException

            try:
                artifact_ref = reference_local_file(
                    physical, scope=scope, user_id=user_id, session_id=_sess
                )
                artifact_ref = {
                    k: artifact_ref[k] for k in ("file_id", "name", "mime_type", "size")
                }
                artifact_ref["url"] = f"/files/{artifact_ref['file_id']}"
            except (HTTPException, OSError, ValueError) as exc:
                return resp_json(
                    {
                        "error": str(getattr(exc, "detail", exc)),
                        "file_path": file_path,
                        "note": "文件已写入，但未交付。请将最终文件保存在当前会话或项目目录内后再 pin。",
                    }
                )
        elif needs_registration:
            artifact_ref = register_file_artifact(
                user_id=user_id,
                chat_id=chat_id,
                filename=basename(physical),
                content=new_bytes,
                scope=scope,
            )

        # Auto-pin to the workspace: makes the card appear in the current turn without the model explicitly calling pin_to_workspace
        if artifact_ref:
            pin_artifact_to_workspace(artifact_ref)

        # ── Response ───────────────────────────────────────────────────
        payload: dict = {
            "ok": True,
            "type": "update" if is_update else "create",
            "file_path": file_path,  # hand back the path the model originally passed in
            "physical_path": physical,
            "size": len(new_bytes),
            **space_metadata(physical, user_id),
        }
        if is_update and original_text is not None:
            payload["diff"] = _make_unified_diff(file_path, original_text, content)
        if artifact_ref:
            payload["artifact"] = artifact_ref
            payload["artifacts"] = [artifact_ref]
            payload["file_id"] = artifact_ref.get("file_id")
            if artifact_ref.get("in_place_update"):
                payload["note"] = (
                    "已就地更新 my-space 中的同名 artifact（file_id 不变，"
                    "Canvas/下载链接保持有效）。"
                )
        return resp_json(payload)

    Write.__doc__ = (
        "创建文件或全量覆盖已存在文件。\n\n"
        "写 ``/myspace/<文件夹>/<文件名>`` 时目录层级会映射到我的空间目录树"
        "（缺失文件夹自动建）。\n\n"
        "只用于新文件或整体重写；小修改优先用 ``Edit``（只发改动片段，比全量重写省 token）。\n\n"
        "Args:\n"
        "    file_path (`str`): 文件路径。默认沙盒 ``/workspace/scratch/<name>``；\n"
        "        仅在用户要求存入其「我的空间」时用 ``/myspace/...``。\n"
        "    content (`str`): 完整文件内容（UTF-8 文本）。\n"
        "    register_as_artifact (`bool`): 默认 false。写在沙盒里又想让用户\n"
        "        能下载该文件时传 true。写入 ``/myspace/`` 后，用户可在网页目录中查看。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, type: 'create'|'update', file_path, physical_path,\n"
        "             size, persistent, diff?, artifact?, file_id?, note?}``\n"
        "    成功；``{error: '...'}`` 失败。\n"
        "    ``persistent=true`` 表示文件保存于个人或团队空间；``space_type`` 为 personal/team/temporary。\n"
    )

    toolkit.append(function_tool(Write))
    logger.info("[factory] Registered Write tool (chat_id=%s)", chat_id)
