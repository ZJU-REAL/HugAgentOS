"""Read tool — sandbox file reading, ported from Claude Code's FileReadTool.

Fallback: sandbox containers have a TTL and get reclaimed after idling → the next
round's ``/workspace`` is completely empty. So the model never has to be aware of
this layer, when ``provider.get_file`` fails ``Read`` looks up a same-named file in
the DB artifact table (same chat, same user); on a hit it pulls the bytes back from
storage and re-``put_file``s them into the sandbox (self-healing), then continues
with the normal read flow.

The model should prefer ``read_artifact(file_id)`` for explicit access to historical
files; this fallback is just a safety net for the "model still thinks in sandbox
paths" scenario.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from agentscope.tool import Toolkit
from core.services.project_scope import ProjectScope

from . import myspace_vfs as _ms
from ._common import resolve_sandbox_session, resp_json
from ._paths import (
    PATH_POLICY_DOC,
    basename,
    is_myspace_physical,
    to_physical_path,
    validate_project_scope_path,
    validate_workspace_path,
)
from ._state import ReadEntry, ReadStateTracker
from .edition_artifact_recovery import recover_organization_artifact

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


def _fallback_recover_from_artifact(
    *,
    file_path: str,
    chat_id: Optional[str],
    user_id: Optional[str],
    scope: Optional[ProjectScope],
) -> Optional[bytes]:
    """If the file is missing from the sandbox, try recovering it from the
    artifact storage. Edition-specific project scopes are handled by their own
    implementation; the shared fallback is restricted to the current user and
    chat.

    Returns the recovered bytes on hit, ``None`` otherwise.
    """
    fname = basename(file_path)
    if not fname:
        return None

    handled, data = recover_organization_artifact(file_path=file_path, scope=scope)
    if handled:
        return data

    if not chat_id or not user_id:
        return None
    try:
        from core.db.engine import SessionLocal
        from core.db.models import Artifact
        from core.storage import get_storage
    except Exception as exc:
        logger.warning("[read.fallback] deps unavailable: %s", exc)
        return None
    db = SessionLocal()
    try:
        row = (
            db.query(Artifact)
            .filter(
                Artifact.chat_id == chat_id,
                Artifact.user_id == user_id,
                Artifact.filename == fname,
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc())
            .first()
        )
        if row is None:
            logger.info(
                "[read.fallback] no artifact match chat=%s user=%s name=%s",
                chat_id,
                user_id,
                fname,
            )
            return None
        try:
            data = get_storage().download_bytes(str(row.storage_key))
            logger.info(
                "[read.fallback] recovered %s from artifact %s (%d bytes)",
                file_path,
                row.artifact_id,
                len(data),
            )
            return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[read.fallback] download_bytes failed for storage_key=%s: %s",
                row.storage_key,
                exc,
            )
            return None
    finally:
        db.close()


def register_read(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    state: ReadStateTracker,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:
    """Register the ``Read`` tool.

    ``state`` is the per-chat ReadStateTracker shared with Edit/Write.
    ``chat_id`` scopes DB artifact recovery; ``sandbox_session_id`` (``None`` →
    fall back to chat_id) selects which sandbox container to read from.
    """

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def Read(
        file_path: str,
        offset: int = 0,
        limit: int = 0,
    ) -> "ToolResponse":  # type: ignore[name-defined]
        from core.sandbox import SandboxConnectError as _SCE
        from core.sandbox import SandboxError as _SE
        from core.sandbox import get_sandbox_provider as _get_provider

        path_err = validate_workspace_path(file_path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(file_path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})

        # Logical path /myspace/... → physical path /workspace/myspace/<uid>/...
        physical = to_physical_path(file_path, user_id)

        provider = _get_provider()
        recovered_from_artifact = False
        try:
            content_bytes = await provider.get_file(_sess, physical, user_id=user_id)
        except _SCE as exc:
            return resp_json({"error": f"沙盒连接失败: {exc}"})
        except _SE as exc:
            data: Optional[bytes] = None
            # First choice: folder-aware "My Space" lazy loading (user-scoped,
            # cross-chat; internally already put_file's back into the sandbox +
            # mirror cache)
            try:
                data = await _ms.materialize_into_sandbox(
                    provider,
                    _sess,
                    user_id,
                    file_path,
                    scope=scope,
                )
            except Exception as mexc:  # noqa: BLE001
                logger.warning("[read] myspace 懒加载失败 %s: %s", file_path, mexc)
            # Fallback: legacy chat-scoped same-name recovery (non-myspace paths take this)
            if data is None:
                data = _fallback_recover_from_artifact(
                    file_path=physical,
                    chat_id=chat_id,
                    user_id=user_id,
                    scope=scope,
                )
                if data is not None:
                    try:
                        await provider.put_file(_sess, physical, data, user_id=user_id)
                    except (_SE, _SCE) as put_exc:
                        logger.warning(
                            "[read.fallback] put_file %s failed (continuing): %s",
                            physical,
                            put_exc,
                        )
            if data is None:
                return resp_json({"error": f"读取失败: {exc}"})
            content_bytes = data
            recovered_from_artifact = True

        # Binary detection
        parsed_fallback = False
        if _is_binary(content_bytes):
            # Office documents in "My Space" (docx/pdf/xlsx/pptx) → fall back to
            # the artifact parsed text (merging the former read_artifact capability,
            # keyed by path rather than file_id)
            parsed_text: Optional[str] = None
            if is_myspace_physical(physical, user_id):
                try:
                    fid = _ms.resolve_file_id(user_id, file_path, scope=scope)
                    if fid:
                        from core.content.artifact_reader import fetch_parsed_text

                        pt = fetch_parsed_text(fid, user_id)
                        if pt:
                            parsed_text = pt
                except Exception as pexc:  # noqa: BLE001
                    logger.warning("[read] 解析文本回退失败 %s: %s", file_path, pexc)
            if parsed_text is None:
                return resp_json(
                    {
                        "type": "binary",
                        "file_path": file_path,
                        "physical_path": physical,
                        "size": len(content_bytes),
                        "hint": (
                            "文件是二进制（如 docx/xlsx/pdf/图片），且不在「我的空间」"
                            "或无法解析。如需让用户下载，使用 sandbox_get_artifact"
                            "(src_path)；如需在沙盒内处理，用 bash 调用命令行工具。"
                        ),
                    }
                )
            # Continue the paginated rendering with the parsed text instead of the raw bytes
            content_bytes = parsed_text.encode("utf-8")
            parsed_fallback = True

        if len(content_bytes) > MAX_TEXT_BYTES:
            return resp_json(
                {
                    "type": "too_large",
                    "file_path": file_path,
                    "physical_path": physical,
                    "size": len(content_bytes),
                    "hint": (
                        f"文件超过 {MAX_TEXT_BYTES} 字节，请用 bash 的 head/tail/sed"
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

        # Parameter normalization
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

        # Slice and render into a line-numbered string
        # Note _format_with_line_numbers expects the substring starting at line `start`
        selected = "\n".join(all_lines[start - 1 : end])
        numbered = _format_with_line_numbers(selected, start, end)

        truncated = end < total_lines

        # Only a "full read" is recorded into state (Edit/Write depend on a full read);
        # under the parsed-text fallback (parsed_fallback) what was read is the
        # **parsed text** of a docx/xlsx, not the raw bytes — Edit/Write must never
        # use it to overwrite a binary document as if it were plain text.
        # Record both the logical and physical paths as keys so they can be mixed later
        if not is_partial and not truncated and not parsed_fallback:
            sha = hashlib.sha256(content_bytes).hexdigest()
            entry = ReadEntry(
                content=content_bytes,
                sha256=sha,
                offset=None,
                limit=None,
            )
        elif parsed_fallback:
            # Parsed-text fallback: record a parsed_doc marker (instead of
            # masquerading as a partial read) so Edit/Write can give an accurate
            # rejection reason and the model doesn't fall into a "just do another
            # full read" loop.
            entry = ReadEntry(
                content=b"",
                sha256="",
                offset=None,
                limit=None,
                parsed_doc=True,
            )
        else:
            entry = ReadEntry(
                content=b"",
                sha256="",
                offset=start,
                limit=eff_limit,
            )
        state.record(file_path, entry)
        if physical != file_path:
            state.record(physical, entry)

        payload = {
            "type": "text",
            "file_path": file_path,
            "physical_path": physical,
            "persistent": is_myspace_physical(physical, user_id),
            "content": numbered,
            "start_line": start,
            "end_line": end,
            "total_lines": total_lines,
            "truncated": truncated,
        }
        if parsed_fallback:
            payload["parsed_text"] = True
            payload["note"] = (
                "这是该二进制文档（docx/pdf/xlsx/pptx）的**解析文本**，不是原始"
                "字节。可直接阅读理解，但不要用 Edit/Write 把它当纯文本覆盖原文件"
                "（会损坏文档）；要改文档请用 bash 调命令行工具处理。"
            )
        if truncated:
            payload["hint"] = (
                f"已截断（显示 {start}-{end}/{total_lines} 行）。"
                f'继续读后续：Read(file_path="{file_path}", offset={end + 1})'
            )
        if recovered_from_artifact:
            payload["recovered_from_artifact"] = True
            payload["note"] = (
                "该文件原沙盒副本已不在（容器被回收），已从历史 artifact 库恢复"
                "并 seed 回沙盒。提示：跨轮访问历史文件建议直接用 read_artifact。"
            )
        return resp_json(payload)

    Read.__doc__ = (
        "读取文本文件，返回带行号的内容（``cat -n`` 风格）。\n\n" + PATH_POLICY_DOC + "\n\n"
        "本工具说明：\n"
        "- 默认读沙盒里的文件（``/workspace/...``）。\n"
        "- 当用户提到他「我的空间」里的文件时，可直接读 ``/myspace/...``：\n"
        "  即使当前沙盒里没有也会**自动按需拉取**（懒加载），无需先 stage。\n"
        "- 读「我的空间」里的二进制文档（docx/pdf/xlsx/pptx）会自动返回**解析\n"
        "  文本**，``parsed_text=true``；其它二进制（图片等）返回 type=binary。\n"
        "- 默认读前 2000 行；超长文件用 ``offset`` (1-indexed) + ``limit`` 分段。\n"
        "- **Read 后内容会被记录**：``Edit`` / ``Write`` 改已存在文件前必须先\n"
        "  完整 Read 一次（offset/limit 为 0），否则会被拒绝。\n\n"
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

    toolkit.register_tool_function(Read, namesake_strategy="override")
    logger.info("[factory] Registered Read tool (chat_id=%s)", chat_id)
