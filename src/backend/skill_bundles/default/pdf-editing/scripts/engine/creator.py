"""PDF generation orchestrator (ported from the minimax-pdf skill pipeline).

Pipeline:  palette → cover.html → render_cover.js (Playwright/Chromium) →
           body.pdf (reportlab + matplotlib) → merge (pypdf) → final PDF.

If Chromium is unavailable the cover degrades to a pure-reportlab single-page
cover so ``create`` never hard-fails (decision: Chromium-primary + fallback).

Public:
    create(spec, output_filename) -> dict
    reformat(input_filename, doc_type, output_filename, ...) -> dict

All file I/O happens inside ``engine.workdir()`` so the in-process
MCP runner can isolate concurrent calls.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ._handle import output_path, workdir
from . import _body, _cover_html, _merge, _palette, _reformat

_RENDER_COVER_JS = Path(__file__).parent / "node_scripts" / "render_cover.js"


class PdfCreateError(RuntimeError):
    """Raised when PDF generation fails irrecoverably."""


# ── cover rendering ────────────────────────────────────────────────────────────

def _resolve_node() -> str | None:
    return shutil.which("node") or shutil.which("nodejs")


def _render_cover_via_chromium(html_path: Path, out_pdf: Path,
                               timeout: float) -> tuple[bool, str]:
    """Render cover.html → cover.pdf via the Playwright node script.

    Returns ``(ok, detail)``. On ANY failure (node/Chromium missing, timeout,
    render error) returns ``(False, reason)`` so the caller degrades to the
    pure-reportlab cover — pdf_create must never hard-fail on the cover.
    """
    node = _resolve_node()
    if not node or not _RENDER_COVER_JS.exists():
        return False, "node or render_cover.js unavailable"
    try:
        proc = subprocess.run(
            [node, str(_RENDER_COVER_JS),
             "--input", str(html_path), "--out", str(out_pdf)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"cover render timed out after {timeout}s"
    if proc.returncode == 0 and out_pdf.is_file() and out_pdf.stat().st_size > 0:
        return True, "ok"
    return False, (proc.stderr.strip() or proc.stdout.strip() or
                   f"exit {proc.returncode}")[:300]


def _render_cover_fallback(tokens: dict[str, Any], out_pdf: Path) -> None:
    """Minimal pure-reportlab cover used when Chromium is unavailable.

    Single A4 page: accent band + title + subtitle + author/date. Plain but
    valid, so pdf_create still returns a usable document.
    """
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    w, h = A4
    c = canvas.Canvas(str(out_pdf), pagesize=A4)
    cover_bg = tokens.get("cover_bg", "#0F1F2E")
    accent = tokens.get("accent", "#00B4A6")
    text_light = tokens.get("text_light", "#F0EDE6")

    c.setFillColor(HexColor(cover_bg))
    c.rect(0, 0, w, h, fill=1, stroke=0)
    c.setFillColor(HexColor(accent))
    c.rect(0, h - 14, w, 14, fill=1, stroke=0)
    c.rect(64, h - 250, 90, 6, fill=1, stroke=0)

    c.setFillColor(HexColor(text_light))
    title = str(tokens.get("title", "Untitled"))
    c.setFont("Helvetica-Bold", 34)
    y = h - 300
    for line in simpleSplit(title, "Helvetica-Bold", 34, w - 128):
        c.drawString(64, y, line)
        y -= 42

    sub = str(tokens.get("subtitle", "") or "")
    if sub:
        c.setFont("Helvetica", 16)
        y -= 8
        for line in simpleSplit(sub, "Helvetica", 16, w - 128):
            c.drawString(64, y, line)
            y -= 22

    meta = "  ·  ".join(
        x for x in (str(tokens.get("author", "")), str(tokens.get("date", ""))) if x
    )
    if meta:
        c.setFont("Helvetica", 11)
        c.drawString(64, 80, meta)
    c.showPage()
    c.save()


# ── image materialization ──────────────────────────────────────────────────────

def _resolve_block_paths(content: list[dict[str, Any]], base: Path) -> None:
    """Rewrite image/figure block paths to absolute paths under the workdir.

    render_body resolves image paths against process cwd, but the MCP runner
    pins workdir via thread-local (no chdir). So any relative path is resolved
    here against ``base`` (the workdir) before rendering.
    """
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") in ("image", "figure"):
            raw = blk.get("path") or blk.get("src")
            if raw and not os.path.isabs(str(raw)):
                blk["path"] = str((base / str(raw)).resolve())


# ── public API ─────────────────────────────────────────────────────────────────

def create(*, spec: dict[str, Any], output_filename: str,
           cover_timeout: float = 60.0) -> dict[str, Any]:
    """Build a print-quality PDF from a consolidated spec.

    spec keys:
        title (required), doc_type (default "report"), author, date,
        subtitle, abstract, accent, cover_bg, cover_image (workdir filename),
        content (required: list of content blocks — 23 supported types).
    """
    if not isinstance(spec, dict):
        raise PdfCreateError("spec must be an object")
    title = str(spec.get("title") or "").strip()
    if not title:
        raise PdfCreateError("spec.title is required")
    content = spec.get("content")
    if not isinstance(content, list) or not content:
        raise PdfCreateError("spec.content must be a non-empty list of blocks")

    doc_type = str(spec.get("doc_type") or "report")
    wd = workdir()

    tokens = _palette.build_tokens(
        title=title,
        doc_type=doc_type,
        author=str(spec.get("author", "")),
        date=str(spec.get("date", "")),
        accent_override=str(spec.get("accent", "")),
        cover_bg_override=str(spec.get("cover_bg", "")),
    )
    if spec.get("subtitle"):
        tokens["subtitle"] = str(spec["subtitle"])
    if spec.get("abstract"):
        tokens["abstract"] = str(spec["abstract"])
    if spec.get("cover_image"):
        ci = str(spec["cover_image"])
        tokens["cover_image"] = ci if os.path.isabs(ci) else str((wd / ci).resolve())

    _resolve_block_paths(content, wd)

    cover_html = wd / "_cover.html"
    cover_pdf = wd / "_cover.pdf"
    body_pdf = wd / "_body.pdf"
    final_pdf = output_path(output_filename)
    final_pdf.parent.mkdir(parents=True, exist_ok=True)

    cover_html.write_text(_cover_html.render(tokens), encoding="utf-8")

    cover_mode = "chromium"
    cover_ok, cover_detail = _render_cover_via_chromium(
        cover_html, cover_pdf, cover_timeout)
    if not cover_ok:
        _render_cover_fallback(tokens, cover_pdf)
        cover_mode = "reportlab_fallback"

    _body.build(tokens, content, str(body_pdf))

    merged = _merge.merge(str(cover_pdf), str(body_pdf), str(final_pdf), title=title)
    if merged.get("status") != "ok":
        raise PdfCreateError(f"merge failed: {merged.get('error')}")

    return {
        "output_filename": output_filename,
        "size_bytes": final_pdf.stat().st_size,
        "pages": merged.get("total_pages"),
        "cover_pattern": tokens.get("cover_pattern"),
        "cover_mode": cover_mode,
        "cover_detail": None if cover_ok else cover_detail,
        "doc_type": doc_type,
        "warnings": merged.get("warnings", []),
    }


def reformat(*, input_filename: str, doc_type: str, output_filename: str,
             title: str = "", author: str = "", date: str = "",
             accent: str = "", cover_timeout: float = 60.0) -> dict[str, Any]:
    """Parse an existing document (md/docx/pdf/txt/json) and re-render as a
    print-quality PDF using the same pipeline as ``create``."""
    src = workdir() / input_filename
    if not src.is_file():
        raise PdfCreateError(f"input file '{input_filename}' not found in workdir")

    blocks, warnings = _reformat.parse_file(str(src))
    if not blocks:
        raise PdfCreateError(
            "could not extract any content from input "
            f"({'; '.join(warnings) or 'empty document'})"
        )

    if not title:
        for blk in blocks:
            if isinstance(blk, dict) and blk.get("type") in ("h1", "h2"):
                title = str(blk.get("text", "")).strip()
                break
        title = title or Path(input_filename).stem

    result = create(
        spec={
            "title": title,
            "doc_type": doc_type or "report",
            "author": author,
            "date": date,
            "accent": accent,
            "content": blocks,
        },
        output_filename=output_filename,
        cover_timeout=cover_timeout,
    )
    result["reformat_warnings"] = warnings
    return result
