"""Read files from the active sandbox."""

from __future__ import annotations
from core.llm.factory.tools.definitions import function_tool

import logging
from typing import Optional

from agentscope.tool import Toolkit, ToolResponse
from core.projects.edition import ProjectScope
from core.models.vision import VisionMode

from ._common import response_metadata, resolve_sandbox_session, resp_image, resp_json
from core.sandbox.workspace.resolution import space_metadata, to_physical_path, validate_project_scope_path, validate_workspace_path

logger = logging.getLogger(__name__)

# Aligned with Claude Code: at most 2000 lines per read by default, for paging large files
DEFAULT_LIMIT = 2000
MAX_LIMIT = 2000
# Above this byte size, go straight to the binary hint without attempting utf-8 decode (OOM guard)
MAX_TEXT_BYTES = 5 * 1024 * 1024  # 5MB


def _is_binary(blob: bytes) -> bool:
    """Heuristic: NUL byte in the first 8KB → binary."""
    sample = blob[:8192]
    return b"\x00" in sample


async def _read_image_as_evidence(content_bytes: bytes, file_path: str) -> Optional[dict]:
    """Transcribe an image file into structured evidence via the vision bridge.

    Returns ``None`` when the bytes are not a recognised image or no vision model is
    configured, so the caller falls through to its existing binary handling.
    """
    try:
        from core.models.vision import sniff_mime

        from .read_image_tool import transcribe_image

        if sniff_mime(content_bytes) is None:
            return None
        payload = await transcribe_image(content_bytes, label=file_path)
        if payload is None:
            return None
        return {**payload, "file_path": file_path, "size": len(content_bytes)}
    except Exception as exc:  # noqa: BLE001 — vision is an enhancement, never a hard failure
        logger.warning("[read] vision transcription failed for %s: %s", file_path, exc)
        return None


async def _image_response(
    content_bytes: bytes, file_path: str, vision_mode: VisionMode
) -> Optional["ToolResponse"]:  # type: ignore[name-defined]
    """Image branch of ``Read``: pixels for a multimodal model, a transcription for a
    text-only one. ``None`` means "not handled here" and falls through to the
    parsed-text / binary handling."""
    if vision_mode == "native":
        from core.models.vision import sniff_mime
        from core.models.vision.service import MAX_IMAGE_BYTES

        mime = sniff_mime(content_bytes)
        if mime is None or len(content_bytes) > MAX_IMAGE_BYTES:
            return None
        return await resp_image(content_bytes, mime, name=file_path, meta={"file_path": file_path})
    if vision_mode == "bridge":
        payload = await _read_image_as_evidence(content_bytes, file_path)
        return resp_json(payload) if payload is not None else None
    return None


_IMAGE_NOTE = {
    "native": "  **图片**（png/jpg/gif/webp）直接以图片返回，你能看到原图；其余返回 type=binary。\n",
    "bridge": "  **图片**返回视觉模型的转写证据（``type=image_evidence``），追问图中细节改用\n"
    "  ``read_image(file_path=..., focus=...)``；其余返回 type=binary。\n",
    "none": "  **图片**当前模型无法读取，返回 type=binary；其余二进制同样返回 type=binary。\n",
}


def _format_with_line_numbers(
    text: str,
    start_line: int,
    end_line: int,
) -> str:
    """``cat -n`` style line numbers: ``     1\\tcontent``."""
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines, start=start_line):
        if i > end_line:
            break
        out.append(f"{i:6d}\t{line}")
    return "\n".join(out)


def register_read(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
    vision_mode: VisionMode = "none",
) -> None:
    """Register the ``Read`` tool.

    ``sandbox_session_id`` selects the container; it defaults to ``chat_id``.
    ``vision_mode`` (see ``core.models.vision.resolve_vision_mode``) decides what an image
    file reads back as: pixels, a vision-bridge transcription, or ``type=binary``.
    """

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def Read(
        file_path: str,
        offset: int = 0,
        limit: int = 0,
    ) -> "ToolResponse":  # type: ignore[name-defined]
        from fastapi import HTTPException
        from core.sandbox import SandboxConnectError as _SCE
        from core.sandbox import SandboxError as _SE
        from core.sandbox import get_sandbox_provider as _get_provider

        path_err = validate_workspace_path(file_path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(file_path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})

        from .project_source_access import current_scope_error

        scope_error = current_scope_error(scope, user_id)
        if scope_error:
            return resp_json(scope_error)

        # Logical path /myspace/... → physical path /workspace/myspace/<uid>/...
        physical = to_physical_path(file_path, user_id, session_id=_sess, scope=scope)

        provider = _get_provider()
        # Desktop mode reads the authorized host path.
        from core.config.local_mode import local_mode_enabled as _local_on

        _local_read: Optional[bytes] = None
        if _local_on():
            try:
                from core.sandbox.permissions.dispatch_models import PermissionEnforcementError, require_local_path_permission

                require_local_path_permission(physical, "read")
                with open(physical, "rb") as _f:
                    _local_read = _f.read()
            except PermissionEnforcementError as exc:
                return resp_json({"error": str(exc), "blocked": True})
            except OSError as exc:
                return resp_json({"error": f"读取文件失败: {exc}"})
        try:
            content_bytes = (
                _local_read
                if _local_read is not None
                else await provider.get_file(_sess, physical, user_id=user_id)
            )
        except HTTPException as exc:
            return resp_json({"error": exc.detail, "status": exc.status_code})
        except _SCE as exc:
            return resp_json({"error": f"沙盒连接失败: {exc}"})
        except _SE as exc:
            return resp_json({"error": f"读取失败: {exc}"})

        # Binary detection
        if _is_binary(content_bytes):
            # Images: a screenshot, a chart the agent just rendered, or a picture in
            # "My Space" must not be a dead end — see _image_response.
            image_response = await _image_response(content_bytes, file_path, vision_mode)
            if image_response is not None:
                return response_metadata(image_response, space_metadata(physical, user_id))
            return resp_json({
                "type": "binary",
                **space_metadata(physical, user_id),
                "file_path": file_path,
                "physical_path": physical,
                "size": len(content_bytes),
                "hint": "使用 Bash 调用文档工具处理此文件。",
            })

        if len(content_bytes) > MAX_TEXT_BYTES:
            return resp_json(
                {
                    "type": "too_large",
                    **space_metadata(physical, user_id),
                    "file_path": file_path,
                    "physical_path": physical,
                    "size": len(content_bytes),
                    "hint": (
                        f"文件超过 {MAX_TEXT_BYTES} 字节，请用 Bash 的 head/tail/sed"
                        "切片，或用 offset/limit 参数分段读取。"
                    ),
                }
            )

        try:
            text = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = content_bytes.decode("utf-8", errors="replace")

        all_lines = text.splitlines()
        total_lines = len(all_lines)

        req_offset = max(0, int(offset or 0))
        req_limit = int(limit or 0)
        is_partial = req_offset > 0 or (req_limit > 0)

        if is_partial:
            start = max(1, req_offset or 1)
            eff_limit = min(req_limit or DEFAULT_LIMIT, MAX_LIMIT)
            end = min(start + eff_limit - 1, total_lines)
        else:
            start = 1
            eff_limit = min(DEFAULT_LIMIT, MAX_LIMIT)
            end = min(eff_limit, total_lines)

        selected = "\n".join(all_lines[start - 1 : end])
        numbered = _format_with_line_numbers(selected, start, end)

        truncated = end < total_lines

        payload = {
            "type": "text",
            "file_path": file_path,
            "physical_path": physical,
            **space_metadata(physical, user_id),
            "content": numbered,
            "start_line": start,
            "end_line": end,
            "total_lines": total_lines,
            "truncated": truncated,
        }
        if truncated:
            payload["hint"] = (
                f"已截断（显示 {start}-{end}/{total_lines} 行）。"
                f'继续读后续：Read(file_path="{file_path}", offset={end + 1})'
            )
        return resp_json(payload)

    Read.__doc__ = (
        "读取文本文件，返回带行号的内容（``cat -n`` 风格）。\n\n"
        "- 二进制文档（docx/pdf/xlsx/pptx）返回 ``type=binary``，用 Bash 调文档工具处理；\n"
        + _IMAGE_NOTE[vision_mode]
        + "- 默认读前 2000 行；超长文件用 ``offset``(1-indexed) + ``limit`` 分段。\n"
        "- 只需读要改的片段：``Edit``/``Write`` 不要求先 Read。\n\n"
        "Args:\n"
        "    file_path (`str`): 文件绝对路径。默认沙盒 ``/workspace/...``；\n"
        "        仅当用户要看他「我的空间」的文件时用 ``/myspace/...``。\n"
        "    offset (`int`): 起始行号（1-indexed），默认 0 表示从头读。\n"
        "    limit (`int`): 最多读取行数，默认 0 = 2000，硬上限 2000。\n\n"
        "Returns:\n"
        "    JSON: ``{type: 'text', file_path, content, start_line, end_line,\n"
        "             total_lines, truncated, hint?}``，或 ``{type: 'binary',\n"
        "             ...}`` / ``{type: 'too_large', ...}`` / ``{error: '...'}``。\n"
    )

    toolkit.append(function_tool(Read))
    logger.info("[factory] Registered Read tool (chat_id=%s)", chat_id)
