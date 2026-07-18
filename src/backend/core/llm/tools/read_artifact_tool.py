"""read_artifact tool — on-demand reading of uploaded artifacts (xlsx/pptx/text).

Extracted from the oversized ``core/llm/tool.py``. Self-contained: all heavy
deps (content parsers, hooks) are imported lazily inside the functions.
``core.llm.tool`` re-exports the public names for backward compatibility.
"""

import logging
from typing import Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse

logger = logging.getLogger(__name__)


_READ_ARTIFACT_DEFAULT_LIMIT = 4000
_READ_ARTIFACT_MAX_LIMIT = 20000


def _is_xlsx_meta(meta: dict) -> bool:
    mime = (meta.get("mime_type") or "").lower()
    name = (meta.get("filename") or "").lower()
    return "spreadsheetml.sheet" in mime or name.endswith(".xlsx")


def _is_pptx_meta(meta: dict) -> bool:
    mime = (meta.get("mime_type") or "").lower()
    name = (meta.get("filename") or "").lower()
    return "presentationml.presentation" in mime or name.endswith(".pptx")


def _probe_xlsx_sheet_names(file_id: str, filename: str) -> tuple[list[str], bytes | None]:
    """Cheaply list sheet names and return the bytes for reuse. Returns
    ``([], None)`` on download failure, ``(names, bytes)`` on success."""
    from core.content.file_parser import parse_xlsx_sheet_names
    from core.llm.hooks import _download_artifact_bytes

    file_bytes = _download_artifact_bytes(file_id, filename, "read_artifact xlsx")
    if file_bytes is None:
        return [], None
    try:
        return parse_xlsx_sheet_names(file_bytes), file_bytes
    except RuntimeError as e:
        logger.warning("read_artifact: list sheets failed for %s: %s", file_id, e)
        return [], file_bytes


async def _resolve_xlsx_text(
    file_id: str,
    sheet_name: Optional[str],
    user_id: Optional[str],
    file_bytes: bytes | None,
) -> str:
    """Resolve xlsx text. ``file_bytes`` is the bytes already downloaded by
    the probe — passed through to fetch_parsed_text on the default path so
    we don't re-download from storage. Raises RuntimeError on bad sheet_name
    or missing bytes when sheet_name is set."""
    from core.content.artifact_reader import fetch_parsed_text
    from core.content.file_parser import parse_xlsx_single_sheet

    if sheet_name:
        if file_bytes is None:
            raise RuntimeError("无法下载文件字节，无法按 sheet 读取")
        return parse_xlsx_single_sheet(file_bytes, sheet_name)
    return fetch_parsed_text(file_id, user_id=user_id, prefetched_bytes=file_bytes)


def _probe_pptx_slide_count(file_id: str, filename: str) -> tuple[int, bytes | None]:
    """Cheaply count slides and return the bytes for reuse. Returns
    ``(0, None)`` on download failure, ``(count, bytes)`` on success."""
    from core.content.file_parser import parse_pptx_slide_count
    from core.llm.hooks import _download_artifact_bytes

    file_bytes = _download_artifact_bytes(file_id, filename, "read_artifact pptx")
    if file_bytes is None:
        return 0, None
    try:
        return parse_pptx_slide_count(file_bytes), file_bytes
    except RuntimeError as e:
        logger.warning("read_artifact: count slides failed for %s: %s", file_id, e)
        return 0, file_bytes


async def _resolve_pptx_text(
    file_id: str,
    slide_index: Optional[int],
    user_id: Optional[str],
    file_bytes: bytes | None,
) -> str:
    """Resolve pptx text via in-process python-pptx. ``file_bytes`` is the
    bytes the probe already downloaded — passed through to fetch_parsed_text
    on the default path so we don't re-download. Raises RuntimeError on
    bad slide_index or missing bytes."""
    from core.content.artifact_reader import fetch_parsed_text
    from core.content.file_parser import parse_pptx_slide

    if slide_index is not None:
        if file_bytes is None:
            raise RuntimeError("无法下载文件字节，无法按 slide_index 读取")
        return parse_pptx_slide(file_bytes, slide_index)
    return fetch_parsed_text(file_id, user_id=user_id, prefetched_bytes=file_bytes)


def register_read_artifact(toolkit: Toolkit, user_id: Optional[str] = None) -> None:
    """Register the read_artifact tool for on-demand reading of uploaded files.

    The tool returns paginated parsed text for an artifact, reading from
    Artifact.parsed_text cache when available (otherwise parses + caches).
    Used to implement cross-turn file access: hooks inject only file summaries
    for historical files, and the agent calls this tool when it needs full
    content.
    """

    async def read_artifact(
        file_id: str,
        offset: int = 0,
        limit: int = _READ_ARTIFACT_DEFAULT_LIMIT,
        sheet_name: str | None = None,
        slide_index: int | None = None,
    ) -> ToolResponse:
        """读取已上传文件的完整解析文本（按字符分页）。

        ⚠️ 重要：每轮对话每个 file_id **累计最多读取 50000 字符**（与文件预览
        预算一致）。超出后会被拒绝并要求改用更合适的工具。

        **Excel/表格类文件优先级**：
        1. **对每行批量执行同一任务** → 用 `batch_plan(file_ids=[...])`，
           而**不是**用 read_artifact 把表读完再自己循环
        2. **多 sheet 工作簿** → 先看返回的 `sheet_names`，再用 `sheet_name`
           精准读单个 sheet（如 `read_artifact(file_id, sheet_name='2024Q3')`）
        3. **抽查特定行/区域** → 用本工具，但请精准指定 offset/limit
        4. **不要**为了"看清全表"反复翻页 —— 全表通常远超 50K 预算

        **PPT/演示文稿**：
        1. **看整体大纲** → 不传 slide_index，返回所有页文本（按页分段）
        2. **细看某一页** → 传 `slide_index`（0 起算，参考 `slide_count`）

        Args:
            file_id (`str`):
                文件 ID（例如 ua_abc123），取自当前对话 [历史已上传文件] 清单或
                当轮附件的 file_id。
            offset (`int`):
                起始字符位置。从 0 开始；结合 next_offset 字段可继续分页。
            limit (`int`):
                本次返回的最大字符数，默认 4000，上限 20000。
            sheet_name (`str`, optional):
                仅 xlsx 有效。指定后只返回该 sheet 的 markdown；不指定则返
                回全部 sheet 拼接。可选值见返回中的 `sheet_names`。
            slide_index (`int`, optional):
                仅 pptx 有效。指定后只返回该页（0 起算）的文本；不指定则返
                回全部页拼接。可选范围 [0, slide_count-1]。

        Returns:
            JSON: 标准字段 {file_id, filename, total_chars, offset,
                  returned_chars, has_more, next_offset, content,
                  read_chars_so_far, budget_remaining}；xlsx 额外含
                  `sheet_names`；pptx 额外含 `slide_count`。
                  失败返回 {error: 原因}。

            ``budget_remaining`` 为 0 时下次调用会被拒绝；如果还需要更多内容，
            改用 ``batch_plan`` 处理整张表。
        """
        import json as _json

        from core.content.artifact_reader import fetch_parsed_text, load_artifact_meta
        from core.llm.hooks import MAX_FILE_CONTENT_CHARS, get_artifact_read_state

        fid = (file_id or "").strip()
        if not fid:
            return ToolResponse(content=[TextBlock(
                type="text",
                text=_json.dumps({"error": "file_id 不能为空"}, ensure_ascii=False),
            )])

        if sheet_name is not None and slide_index is not None:
            return ToolResponse(content=[TextBlock(
                type="text",
                text=_json.dumps(
                    {"error": "sheet_name 与 slide_index 互斥（分别用于 xlsx / pptx）"},
                    ensure_ascii=False,
                ),
            )])

        meta = load_artifact_meta(fid, user_id=user_id)
        if meta is None:
            return ToolResponse(content=[TextBlock(
                type="text",
                text=_json.dumps(
                    {"error": f"文件 {fid} 不存在、已删除，或无权访问"},
                    ensure_ascii=False,
                ),
            )])

        # File-type-aware text resolution. The probe runs BEFORE parsing
        # so that error responses (bad sheet_name / out-of-range slide_index)
        # can still surface sheet_names / slide_count to help the model
        # self-correct on the next call. Bytes downloaded by the probe are
        # threaded through to the resolver to avoid a second storage hit.
        extras: dict = {}
        try:
            if _is_xlsx_meta(meta):
                if slide_index is not None:
                    raise RuntimeError("slide_index 仅对 pptx 文件有效")
                sheet_names, file_bytes = _probe_xlsx_sheet_names(
                    fid, meta.get("filename") or "file.xlsx"
                )
                extras["sheet_names"] = sheet_names
                text = await _resolve_xlsx_text(fid, sheet_name, user_id, file_bytes)
            elif _is_pptx_meta(meta):
                if sheet_name is not None:
                    raise RuntimeError("sheet_name 仅对 xlsx 文件有效")
                slide_count, file_bytes = _probe_pptx_slide_count(
                    fid, meta.get("filename") or "file.pptx"
                )
                extras["slide_count"] = slide_count
                if slide_index is not None:
                    extras["slide_index"] = slide_index
                text = await _resolve_pptx_text(fid, slide_index, user_id, file_bytes)
            else:
                if sheet_name is not None or slide_index is not None:
                    raise RuntimeError(
                        "sheet_name / slide_index 仅对 xlsx / pptx 文件有效"
                    )
                text = fetch_parsed_text(fid, user_id=user_id)
        except RuntimeError as e:
            err = {"error": str(e), "file_id": fid, "filename": meta.get("filename")}
            err.update(extras)
            return ToolResponse(content=[TextBlock(
                type="text", text=_json.dumps(err, ensure_ascii=False),
            )])

        if not text:
            error = meta.get("parse_error") or "文件暂无可读文本内容"
            err = {"error": error, "filename": meta.get("filename"), "file_id": fid}
            err.update(extras)
            return ToolResponse(content=[TextBlock(
                type="text", text=_json.dumps(err, ensure_ascii=False),
            )])

        total = len(text)
        try:
            off = max(0, int(offset))
        except (TypeError, ValueError):
            off = 0
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = _READ_ARTIFACT_DEFAULT_LIMIT
        lim = max(1, min(lim, _READ_ARTIFACT_MAX_LIMIT))

        # ── Per-turn cumulative budget guard ──────────────────────────────
        # The budget mirrors the static file_context injection cap so the
        # model can never read more (in one turn) than what would have been
        # statically injected. Prevents context explosion when a curious
        # model tries to traverse a huge spreadsheet sequentially.
        state = get_artifact_read_state()
        already_read = state.get(fid, 0)
        budget_remaining = max(0, MAX_FILE_CONTENT_CHARS - already_read)

        if budget_remaining <= 0:
            mime = (meta.get("mime_type") or "").lower()
            filename = (meta.get("filename") or "").lower()
            is_xlsx = "spreadsheetml.sheet" in mime or filename.endswith(".xlsx")
            hint = (
                f"该文件本轮已累计读取 {already_read} 字符，达到 "
                f"{MAX_FILE_CONTENT_CHARS} 字符的单文件读取上限。"
            )
            if is_xlsx:
                hint += (
                    f" 如需对每行批量执行任务，请改用 batch_plan(file_ids=['{fid}'])"
                    "，后端会读取全部行；不要继续用 read_artifact 翻完整张表。"
                )
            else:
                hint += " 如需更多内容，请缩小范围（精准 offset/limit）后再试。"
            return ToolResponse(content=[TextBlock(
                type="text",
                text=_json.dumps(
                    {
                        "error": hint,
                        "file_id": fid,
                        "filename": meta.get("filename"),
                        "total_chars": total,
                        "read_chars_so_far": already_read,
                        "budget_remaining": 0,
                    },
                    ensure_ascii=False,
                ),
            )])

        # Clamp this call's slice to whatever budget is left.
        effective_lim = min(lim, budget_remaining)

        if off >= total:
            content_slice = ""
            next_offset = total
        else:
            content_slice = text[off: off + effective_lim]
            next_offset = off + len(content_slice)

        # Update tracker AFTER computing the slice.
        state[fid] = already_read + len(content_slice)
        budget_remaining_after = max(
            0, MAX_FILE_CONTENT_CHARS - state[fid]
        )

        result = {
            "file_id": fid,
            "filename": meta.get("filename"),
            "mime_type": meta.get("mime_type"),
            "total_chars": total,
            "offset": off,
            "returned_chars": len(content_slice),
            "has_more": next_offset < total,
            "next_offset": next_offset if next_offset < total else None,
            "content": content_slice,
            "read_chars_so_far": state[fid],
            "budget_remaining": budget_remaining_after,
        }
        result.update(extras)
        return ToolResponse(content=[TextBlock(
            type="text",
            text=_json.dumps(result, ensure_ascii=False),
        )])

    toolkit.register_tool_function(read_artifact, namesake_strategy="override")
    logger.info("[factory] Registered read_artifact tool (user=%s)", user_id)

