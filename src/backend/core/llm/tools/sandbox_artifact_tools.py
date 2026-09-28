"""Sandbox-backed agent tools: ``bash`` + artifact staging.

- ``bash``: run a shell command inside the per-chat sandbox container.
- ``sandbox_put_artifact``: copy an existing artifact's bytes into the sandbox.
- ``sandbox_get_artifact``: read a sandbox file and register it as a
  downloadable artifact.

Relocated from the former ``core.llm.tool`` module so the singular ``tool.py``
no longer coexists with this ``tools/`` package.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse
from core.llm.tools._common import resolve_sandbox_session
from core.llm.tools._tool_helpers import (
    _resolve_artifact_files,
    _resp_json,
    _store_generated_file_path,
    _validate_workspace_path,
)

logger = logging.getLogger(__name__)

def register_sandbox_put_artifact(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Stage an artifact (user upload or previous output) into the sandbox FS."""
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        return

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def sandbox_put_artifact(artifact_id: str, dest_path: str) -> ToolResponse:
        from core.sandbox import SandboxConnectError as _SandboxConnectError
        from core.sandbox import SandboxError as _SandboxError
        from core.sandbox import get_sandbox_provider as _get_provider

        if not artifact_id or not isinstance(artifact_id, str):
            return _resp_json({"error": "artifact_id 必须为非空字符串"})

        from ._paths import to_physical_path, workspace_directory

        dest_path = to_physical_path(dest_path, user_id, session_id=_sess)
        path_err = _validate_workspace_path(dest_path, root=workspace_directory(_sess))
        if path_err:
            return _resp_json({"error": path_err})

        # _resolve_artifact_files accepts the {filename: artifact_id} shape;
        # using dest_path as the key is fine — it is only the key of the returned dict.
        files_b64, err = _resolve_artifact_files({dest_path: artifact_id}, user_id)
        if err:
            return _resp_json({"error": err})
        if not files_b64:
            return _resp_json({"error": f"artifact '{artifact_id}' 解析失败"})

        try:
            content = base64.b64decode(files_b64[dest_path])
        except Exception as exc:  # noqa: BLE001
            return _resp_json({"error": f"artifact 字节解码失败: {exc}"})

        provider = _get_provider()
        try:
            await provider.put_file(_sess, dest_path, content, user_id=user_id)
        except (_SandboxError, _SandboxConnectError) as exc:
            return _resp_json({"error": str(exc)})

        return _resp_json(
            {
                "ok": True,
                "artifact_id": artifact_id,
                "dest_path": dest_path,
                "size": len(content),
            }
        )

    sandbox_put_artifact.__doc__ = (
        "把已存在的 artifact（用户上传的、或之前产出的文件）拷贝到沙盒路径，\n"
        "供 bash/脚本读取处理。\n\n"
        "Args:\n"
        "    artifact_id (`str`): artifact 的 file_id（如 ua_xxx）。必须属于当前用户。\n"
        "    dest_path (`str`): 当前工作目录内的绝对路径或相对路径，\n"
        "        不允许包含 .. 路径段。父目录会自动创建。\n\n"
        "Returns:\n"
        "    JSON: {ok: true, artifact_id, dest_path, size} 成功；\n"
        "    {error: '...'} 失败（artifact 不存在、无权访问、写入失败等）。\n"
        "限制：单个 artifact 最大 10 MB。\n"
    )

    toolkit.register_tool_function(sandbox_put_artifact, namesake_strategy="override")
    logger.info("[factory] Registered sandbox_put_artifact tool (chat_id=%s)", chat_id)


def register_sandbox_get_artifact(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    scope: Optional["ProjectScope"] = None,
) -> None:
    """Read a sandbox file and register it as a downloadable artifact."""
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        return

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)
    from core.config.settings import settings as _settings

    max_bytes = _settings.sandbox.artifact_max_bytes

    async def sandbox_get_artifact(src_path: str, name: str = "") -> ToolResponse:
        import mimetypes as _mt

        from core.sandbox import SandboxConnectError as _SandboxConnectError
        from core.sandbox import SandboxError as _SandboxError
        from core.sandbox import get_sandbox_provider as _get_provider

        from core.config.local_mode import local_mode_enabled
        from ._paths import to_physical_path

        if local_mode_enabled():
            from core.artifacts.local_project import reference_project_file, is_project_file_path
            from fastapi import HTTPException

            local_scope = scope
            if local_scope and local_scope.is_local:
                physical = to_physical_path(src_path, user_id, session_id=_sess)
                # Project files are already durable. Only scratch exports need a copy.
                if is_project_file_path(src_path, local_scope) or is_project_file_path(physical, local_scope):
                    try:
                        ref = await asyncio.to_thread(
                            reference_project_file, physical, scope=local_scope,
                            user_id=user_id or "", name=name,
                        )
                    except (HTTPException, OSError, ValueError) as exc:
                        return _resp_json({"error": str(getattr(exc, "detail", exc))})
                    ref = {k: ref[k] for k in ("file_id", "name", "mime_type", "size")}
                    ref["url"] = f"/files/{ref['file_id']}"
                    return _resp_json({"ok": True, **ref, "artifacts": [ref]})

        from ._paths import to_physical_path, workspace_directory

        src_path = to_physical_path(src_path, user_id, session_id=_sess)
        path_err = _validate_workspace_path(src_path, root=workspace_directory(_sess))
        if path_err:
            return _resp_json({"error": path_err})

        provider = _get_provider()
        from core.sandbox import SandboxFileTooLargeError as _SandboxFileTooLargeError

        suffix = Path(src_path).suffix
        with tempfile.NamedTemporaryFile(
            prefix="sandbox-artifact-", suffix=suffix, delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            size = await provider.get_file_to_path(
                _sess,
                src_path,
                tmp_path,
                max_bytes=max_bytes,
                user_id=user_id,
            )
        except _SandboxFileTooLargeError as exc:
            tmp_path.unlink(missing_ok=True)
            suggestion = "PDF 请按页拆分为多个文件后逐个交付；其他格式请拆包或降低内容体积。"
            return _resp_json(
                {
                    "error": (
                        f"文件 {src_path} 过大: {exc.actual_size} bytes > " f"{exc.max_size} bytes"
                    ),
                    "code": "sandbox_artifact_too_large",
                    "actual_size": exc.actual_size,
                    "max_size": exc.max_size,
                    "suggestion": suggestion,
                }
            )
        except (_SandboxError, _SandboxConnectError) as exc:
            tmp_path.unlink(missing_ok=True)
            return _resp_json({"error": str(exc)})
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        try:
            if size <= 0:
                return _resp_json({"error": f"文件 {src_path} 为空"})

            out_name = (name or Path(src_path).name).strip() or "output"
            mime, _ = _mt.guess_type(out_name)
            mime = mime or "application/octet-stream"

            ref = await asyncio.to_thread(
                _store_generated_file_path,
                tmp_path,
                name=out_name,
                mime_type=mime,
                user_id=user_id,
                source="sandbox_get_artifact",
                extra_metadata={"src_path": src_path} if src_path else None,
            )
            if not ref:
                return _resp_json({"error": "artifact 登记失败（存储后端不可用？）"})

            return _resp_json(
                {
                    "ok": True,
                    "file_id": ref["file_id"],
                    "name": ref["name"],
                    "url": ref["url"],
                    "mime_type": ref["mime_type"],
                    "size": ref["size"],
                    # frontend ToolOutputRenderer expects download links rendered as an artifacts array
                    "artifacts": [ref],
                }
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    sandbox_get_artifact.__doc__ = (
        "登记文件并返回 file_id。本机项目文件只引用原文件，不复制到 artifacts；临时沙盒文件才导出保存。\n\n"
        "⚠️ **登记 ≠ 交付**：返回的 url 默认对用户隐藏，必须再调\n"
        "`pin_to_workspace(file_ids=[...])` 文件才作为附件出现在对话区；\n"
        "**禁止**把 file_id 或 url 写进正文当下载链接。\n\n"
        "Args:\n"
        "    src_path (`str`): 工作目录内的绝对或相对路径，或当前本机项目中的真实绝对路径。\n"
        "    name (`str`, 可选): 用户面向的文件名。不传则取 src_path 的 basename。\n\n"
        "Returns:\n"
        "    JSON: {ok: true, file_id, name, url, mime_type, size, artifacts: [...]}\n"
        "    或 {error: '...'}。\n"
        f"限制：单文件最大 {max_bytes} bytes（默认 100 MiB，可由 "
        "SANDBOX_ARTIFACT_MAX_BYTES 配置）。超限时不要反复尝试同一文件；"
        "PDF 应按页拆分，其他格式应拆包或降低体积后再逐个登记。\n"
    )

    toolkit.register_tool_function(sandbox_get_artifact, namesake_strategy="override")
    logger.info("[factory] Registered sandbox_get_artifact tool (chat_id=%s)", chat_id)
