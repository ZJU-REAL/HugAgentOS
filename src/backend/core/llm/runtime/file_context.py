"""Turn file attachments into context: file handles for documents, base64 for images."""
from __future__ import annotations
from core.files import artifact_bytes
import base64
from typing import Any, Dict, List, Optional
import logging
logger = logging.getLogger(__name__)

def fetch_image_base64(
    f: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Fetch image data for a file attachment and return (base64_data, mime_type)."""
    file_id = f.get("file_id") or ""
    if not file_id:
        return None
    mime_type = (f.get("mime_type") or "image/png").lower()
    raw = artifact_bytes.download_artifact_bytes(
        file_id,
        f.get("name") or "image",
        "Image hook",
        user_id=user_id,
    )
    if raw is None:
        return None
    return base64.b64encode(raw).decode("utf-8"), mime_type


# Rough size of one attachment handle block, reported to the frontend so its
# context gauge estimates what the backend actually injects for a staged file.
ATTACHMENT_HANDLE_EST_CHARS = 300


def build_file_context(uploaded_files: List[Dict[str, Any]]) -> str:
    """Describe the turn's non-image attachments as handles: name, MIME and ``file_id``.

    No file is downloaded or parsed here, so attaching a large PDF adds nothing
    to time-to-first-token. The model reads content on demand through
    ``read_artifact``, which owns parsing, the ``parsed_text`` cache and the
    ``user_id`` ownership check.
    """
    text_files = [f for f in uploaded_files if not artifact_bytes.is_image(f)]
    if not text_files:
        return ""

    lines: List[str] = []
    for f in text_files:
        name = f.get("name") or f.get("filename") or "未知文件"
        fid = (f.get("file_id") or "").strip()
        mime_type = f.get("mime_type") or "(未知)"
        if not fid:
            lines.append(f"- {name}  (MIME: {mime_type})  [缺少 file_id，无法读取]")
            continue
        lines.append(f"- {name}  (file_id: {fid}, MIME: {mime_type})")

    return (
        "[用户上传的附件]（本轮只提供文件元数据，正文尚未读取）\n"
        + "\n".join(lines)
        + "\n\n[读取指引]\n"
        "- 需要文件内容时调用 read_artifact(file_id, offset=0, limit=4000) 读取解析后的文本，"
        "按返回的 next_offset 继续分页\n"
        "- xlsx 先看返回的 sheet_names 再用 sheet_name 读单表；pptx 用 slide_index 读单页\n"
        "- 调用 read_artifact 之前不要声称已读过附件，也不要推断其内容；"
        "与附件无关的问题可以不读"
    )


def describe_source(source: str) -> str:
    from core.files.artifacts.reader import SOURCE_AI_GENERATED, SOURCE_USER_UPLOAD

    return {SOURCE_USER_UPLOAD: "用户上传", SOURCE_AI_GENERATED: "AI 生成"}.get(source, "")


def build_historical_files_context(historical_files: List[Dict[str, Any]]) -> str:
    """Build a compact summary block for files from previous turns.

    Includes both user-uploaded files and AI-generated files (reports,
    charts, code output, etc.) produced by tools in earlier turns.
    Each entry is labeled by provenance so the agent knows what kind of
    file it's referencing.

    Agent gets only `{filename, file_id, source, summary}`; full content
    is fetched on-demand via the `read_artifact` tool. This keeps prompt
    size bounded as the conversation grows.
    """
    if not historical_files:
        return ""

    lines: List[str] = []
    for f in historical_files:
        file_id = f.get("file_id") or ""
        name = f.get("name") or f.get("filename") or "未命名文件"
        mime = f.get("mime_type") or ""
        summary = (f.get("summary") or "").strip()
        source = f.get("source") or ""
        deleted = bool(f.get("deleted"))

        source_label = describe_source(source)
        header_parts = [f"file_id: {file_id}"]
        if source_label:
            header_parts.append(source_label)
        if mime:
            header_parts.append(mime)
        header = f"- {name}  ({', '.join(header_parts)})"

        if deleted:
            lines.append(header + "  [文件已删除，无法读取]")
            continue

        lines.append(header)
        # Key point: tell the model the stable /myspace/<name> path so it can operate
        # directly with Read/Edit/Write — better suited than read_artifact for
        # "modify HTML/CSS/MD/source code" text-editing scenarios, and changes sync
        # back to "My Space" immediately.
        if name and source == ai_source():
            lines.append(f"  沙盒路径: /myspace/{name}   ← 用 Read/Edit/Write 直接读改写")
        if summary:
            indented = "\n".join(f"  {ln}" for ln in summary.splitlines())
            lines.append(f"  摘要：\n{indented}")
        else:
            lines.append("  摘要：（尚未生成）")

    header = (
        "[历史文件清单]（本会话中之前轮次涉及的文件，包含用户上传和 AI 生成的两类，以下仅为摘要）\n"
        "访问方式：\n"
        '- **要修改文本/HTML/MD/代码** → 用 `Read("/myspace/<filename>")` 读完整内容，'
        "再用 `Edit` / `Write` 改；改完会自动同步回「我的空间」（同一 file_id）。\n"
        "- **要看 docx/pdf/xlsx 等需要解析的二进制文件** → 用 "
        "`read_artifact(file_id, offset, limit)` 拿解析后的文本。"
    )
    return header + "\n\n" + "\n".join(lines)


def ai_source() -> str:
    """Lazy resolve to avoid circular import at module load."""
    from core.files.artifacts.reader import SOURCE_AI_GENERATED

    return SOURCE_AI_GENERATED
