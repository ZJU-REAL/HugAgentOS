"""parser extract responsibilities."""
from __future__ import annotations
import re
import uuid
from typing import Optional
import logging
logger = logging.getLogger(__name__)
from features.knowledge import kb_parser_state
from features.knowledge import parser_types

def extract_text(file_bytes: bytes, mime_type: str) -> list[dict]:
    """Extract structured paragraphs from a document.

    Delegates to core/file_parser.py for PDF, DOCX, DOC, TXT (uses the
    external file-parser API for PDF, pandoc for DOCX, etc.).
    Falls back to built-in xlsx/plain-text extraction for unsupported types.

    Returns a list of paragraph dicts:
        {"text": str, "heading_level": int|None, "element_type": str}
    """
    paragraphs, _ = extract_text_with_assets(file_bytes, mime_type, with_assets=False)
    return paragraphs


def extract_text_with_assets(
    file_bytes: bytes,
    mime_type: str,
    *,
    with_assets: bool = True,
) -> tuple[list[dict], list[parser_types.RawAsset]]:
    """Extract paragraphs and, when ``with_assets``, the media the document carries.

    Asset extraction is opt-in because it costs a heavier parse call: the layout
    service inlines every figure as base64 in the JSON response, which the text-only
    callers have no use for. When the rich call fails for any reason we fall back to
    the plain text path — a parse service that doesn't support the media params must
    degrade to today's behaviour, never fail the document.
    """
    mt = (mime_type or "").lower()

    # Standalone image upload. Without this branch an image falls through to
    # ``_extract_plain_text``, which latin-1 decodes the binary into garbage
    # paragraphs and indexes them — the upload allowlist accepts png/jpg/webp/gif
    # (``core/files/content/file_validation.py``) so this path is reachable in production.
    if mt.startswith("image/"):
        return extract_standalone_image(file_bytes, mt, with_assets=with_assets)

    # XLSX — keep built-in handler (file_parser doesn't support spreadsheets)
    if mt in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ):
        return extract_xlsx(file_bytes), []

    if with_assets and mt == "application/pdf":
        rich = extract_pdf_rich(file_bytes)
        if rich is not None:
            return rich

    # Try core/file_parser for all other known types
    suffix = kb_parser_state.MIME_TO_SUFFIX.get(mt)
    if suffix:
        try:
            from core.files.content.file_parser import parse_file
            filename = f"upload{suffix}"
            content = parse_file(file_bytes, filename)
            if content:
                return markdown_to_paragraphs(content), []
        except RuntimeError:
            # file_parser failed (e.g. service down) — fall through to built-in
            pass

    # Fallback: treat as plain text
    return extract_plain_text(file_bytes), []


def extract_standalone_image(
    file_bytes: bytes,
    mime_type: str,
    *,
    with_assets: bool,
) -> tuple[list[dict], list[parser_types.RawAsset]]:
    """An uploaded image is one asset with one synthetic paragraph holding its ref.

    The paragraph exists so the normal chunker still produces a parent chunk: retrieval
    fetches parent content from PostgreSQL, and an image-only document with no chunk
    would be unreachable. The caption/description is attached to the asset, not
    inlined here — it is not known until the VLM pass has run.
    """
    placeholder = f"asset://{uuid.uuid4().hex[:16]}"
    paragraphs = [{"text": f"![]({placeholder})", "heading_level": None, "element_type": "image"}]
    if not with_assets:
        return paragraphs, []
    asset = parser_types.RawAsset(
        data=file_bytes,
        placeholder=placeholder,
        kind="image",
        mime_type=mime_type,
        index=0,
    )
    return paragraphs, [asset]


def extract_pdf_rich(file_bytes: bytes) -> Optional[tuple[list[dict], list[parser_types.RawAsset]]]:
    """Parse a PDF keeping its figures. Returns None to signal "fall back to text-only"."""
    try:
        from core.files.content.pdf_parser import parse_pdf_rich

        result = parse_pdf_rich(file_bytes, "upload.pdf")
    except Exception as exc:
        logger.warning("PDF 富解析失败，退回纯文本解析: %s", exc)
        return None

    paragraphs = markdown_to_paragraphs(result.markdown)
    if not result.images:
        return paragraphs, []

    # Layout metadata (caption / bbox / page) keyed by the same path the markdown
    # references, so each figure's asset carries where it came from.
    meta_by_path: dict[str, dict] = {}
    for block in result.blocks:
        if block.get("type") != "image":
            continue
        path = str(block.get("img_path") or "")
        if path:
            meta_by_path[path] = block

    assets: list[parser_types.RawAsset] = []
    seen: set[str] = set()
    # Iterate in document order: the markdown's image links are the source of truth for
    # ordering, and only images actually referenced by the text get an asset.
    for idx, path in enumerate(kb_parser_state.MD_IMAGE_RE.findall(result.markdown)):
        if path in seen:
            continue
        data = result.images.get(path)
        if not data:
            continue
        seen.add(path)
        meta = meta_by_path.get(path, {})
        caption_parts = [
            str(x).strip()
            for x in (meta.get("image_caption") or []) + (meta.get("image_footnote") or [])
            if str(x).strip()
        ]
        locator = {}
        if meta.get("page_idx") is not None:
            locator["page_idx"] = meta.get("page_idx")
        if meta.get("bbox"):
            locator["bbox"] = meta.get("bbox")
        assets.append(
            parser_types.RawAsset(
                data=data,
                placeholder=path,
                kind="image",
                mime_type=guess_image_mime(path),
                text_content=" ".join(caption_parts),
                locator=locator,
                index=idx,
            )
        )

    return paragraphs, assets


def guess_image_mime(path: str) -> str:
    lowered = path.lower()
    for mime, ext in kb_parser_state.IMAGE_MIME_TO_EXT.items():
        if lowered.endswith(ext):
            return mime
    if lowered.endswith(".jpeg"):
        return "image/jpeg"
    return "image/jpeg"


def image_extension_for_mime(mime_type: str) -> str:
    """Filename suffix for an image mime, used when building the storage key."""
    return kb_parser_state.IMAGE_MIME_TO_EXT.get((mime_type or "").lower(), ".jpg")


def markdown_to_paragraphs(text: str) -> list[dict]:
    """Parse markdown text (from file_parser) into structured paragraph dicts."""
    paragraphs: list[dict] = []
    table_buffer: list[str] = []

    def _flush_table():
        if table_buffer:
            paragraphs.append({
                "text": "\n".join(table_buffer),
                "heading_level": None,
                "element_type": "table",
            })
            table_buffer.clear()

    for line in text.splitlines():
        stripped = line.strip()

        # Empty line — flush table buffer, skip
        if not stripped:
            _flush_table()
            continue

        # Markdown table row
        if stripped.startswith("|") and stripped.endswith("|"):
            table_buffer.append(stripped)
            continue

        # Not a table row — flush any pending table
        _flush_table()

        # Markdown heading
        md_match = re.match(r'^(#{1,6})\s+(.*)', stripped)
        if md_match:
            level = len(md_match.group(1))
            paragraphs.append({
                "text": stripped,
                "heading_level": level,
                "element_type": "heading",
            })
            continue

        # Chinese legal/report heading patterns
        heading_level = detect_heading_level(stripped)
        if heading_level:
            paragraphs.append({
                "text": stripped,
                "heading_level": heading_level,
                "element_type": "heading",
            })
            continue

        # Regular paragraph
        paragraphs.append({
            "text": stripped,
            "heading_level": None,
            "element_type": "paragraph",
        })

    _flush_table()
    return paragraphs


def extract_xlsx(file_bytes: bytes) -> list[dict]:
    import io
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl is required: pip install openpyxl")

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    paragraphs = []
    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        header = [str(c or "") for c in rows[0]]
        paragraphs.append({
            "text": f"## Sheet: {sheet.title}",
            "heading_level": 2,
            "element_type": "heading",
        })
        md_lines = ["| " + " | ".join(header) + " |"]
        md_lines.append("|" + "|".join("---" for _ in header) + "|")
        for row in rows[1:]:
            if all(c is None for c in row):
                continue
            md_lines.append("| " + " | ".join(str(c or "") for c in row) + " |")
        paragraphs.append({
            "text": "\n".join(md_lines),
            "heading_level": None,
            "element_type": "table",
        })
    return paragraphs


def extract_plain_text(file_bytes: bytes) -> list[dict]:
    """Fallback: decode as text and split into paragraphs."""
    # Try common encodings
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = file_bytes.decode("utf-8", errors="replace")

    return markdown_to_paragraphs(text)


def detect_heading_level(line: str) -> Optional[int]:
    """Detect Chinese legal/report heading patterns."""
    patterns = [
        (r'^第[一二三四五六七八九十百]+章', 1),
        (r'^第[一二三四五六七八九十百]+节', 2),
        (r'^第[一二三四五六七八九十百\d]+条', 3),
        (r'^[一二三四五六七八九十]+[、．.]', 2),
        (r'^\d+[、．.]\s*\S', 3),
    ]
    for pattern, level in patterns:
        if re.match(pattern, line):
            return level
    return None
