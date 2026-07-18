"""PDF reading: text extraction, outline, metadata."""
from __future__ import annotations

from typing import Any

from ._handle import input_path


def _parse_page_range(pages: list[int] | None, total: int) -> list[int]:
    """Normalize a 1-based page list to a 0-based valid index list."""
    if not pages:
        return list(range(total))
    out: list[int] = []
    for p in pages:
        if not isinstance(p, int):
            raise ValueError(f"page numbers must be integers, got {p!r}")
        if not (1 <= p <= total):
            raise ValueError(f"page {p} out of range (PDF has {total} pages)")
        out.append(p - 1)
    return out


def get_text(
    *,
    input_filename: str,
    pages: list[int] | None = None,
) -> dict[str, Any]:
    """Extract text from selected (1-based) pages of a PDF.

    Args:
        input_filename: source PDF in cwd
        pages: optional 1-based page numbers. None / empty = all pages.

    Returns:
        ``{"page_count", "selected_pages", "text",
           "per_page": [{"page", "char_count"}]}``

        ``text`` joins all selected pages with ``\\n\\n`` between pages.
    """
    import pdfplumber

    src = str(input_path(input_filename))
    with pdfplumber.open(src) as pdf:
        total = len(pdf.pages)
        indices = _parse_page_range(pages, total)
        per_page: list[dict[str, Any]] = []
        chunks: list[str] = []
        for idx in indices:
            page = pdf.pages[idx]
            text = page.extract_text() or ""
            chunks.append(text)
            per_page.append({"page": idx + 1, "char_count": len(text)})

    return {
        "page_count": total,
        "selected_pages": [i + 1 for i in indices],
        "text": "\n\n".join(chunks),
        "per_page": per_page,
    }


def get_outline(*, input_filename: str) -> dict[str, Any]:
    """Return the PDF's bookmarks / TOC as a flat list with depth.

    Each entry: ``{"title": str, "page": int (1-based), "level": int}``.
    Empty list if the PDF has no bookmarks.
    """
    from pypdf import PdfReader

    pdf_reader = PdfReader(str(input_path(input_filename)))
    flat: list[dict[str, Any]] = []

    def _walk(items, level: int = 0):
        for it in items:
            if isinstance(it, list):
                _walk(it, level + 1)
                continue
            try:
                page_num = pdf_reader.get_destination_page_number(it) + 1  # 1-based
            except Exception:
                page_num = 0
            title = getattr(it, "title", "") or ""
            flat.append({"title": title, "page": page_num, "level": level})

    try:
        _walk(pdf_reader.outline)
    except Exception:
        # Some PDFs have no /Outlines entry — silently return empty list
        pass

    return {"bookmark_count": len(flat), "outline": flat}


def get_metadata(*, input_filename: str) -> dict[str, Any]:
    """Return PDF document info: page_count, title, author, etc."""
    from pypdf import PdfReader

    src = str(input_path(input_filename))
    reader = PdfReader(src)
    info = reader.metadata or {}

    def _get(key: str) -> str | None:
        # pypdf metadata keys begin with `/`; expose as plain strings.
        v = info.get(key) if hasattr(info, "get") else None
        return str(v) if v is not None else None

    return {
        "page_count": len(reader.pages),
        "title": _get("/Title"),
        "author": _get("/Author"),
        "subject": _get("/Subject"),
        "creator": _get("/Creator"),
        "producer": _get("/Producer"),
        "is_encrypted": reader.is_encrypted,
    }
