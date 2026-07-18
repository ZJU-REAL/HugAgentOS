"""Render a .pptx as per-slide JPG thumbnails.

Pipeline: pptx → PDF (LibreOffice headless) → JPG per page (pdftoppm).
Both binaries are already installed in the mcp container.

Called from the ``thumbnails`` subcommand of the skill's CLI to support
the visual QA loop: agent generates PPT → renders thumbnails → a
fresh-eyes pass visually inspects the JPGs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import _shims as libreoffice  # to_pdf lives here
from ._shims import input_path, output_path


def render_thumbnails(
    *,
    input_filename: str,
    output_prefix: str = "slide",
    dpi: int = 120,
    quality: int = 85,
) -> dict[str, Any]:
    """Render every slide of ``input_filename`` as ``output_prefix-NN.jpg``.

    Args:
        input_filename: source .pptx in workdir
        output_prefix: file prefix (e.g. ``"slide"`` produces ``slide-01.jpg``,
            ``slide-02.jpg``, ...)
        dpi: rasterization resolution (120 ≈ readable thumbnail; 150+ for crisp
             inspection)
        quality: JPEG quality 1-100 (default 85)

    Returns:
        ``{"slide_count": int, "thumbnails": [{"slide_index": 1, "filename": "..."}],
            "size_bytes_total": int}``

    Raises:
        FileNotFoundError if pdftoppm isn't on PATH
        RuntimeError on any step failure
    """
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError(
            "pdftoppm not found in PATH; install poppler-utils in the mcp container"
        )

    # Step 1: pptx → PDF via LibreOffice (using existing helper, which itself
    # calls input_path / output_path — so we operate inside workdir).
    pdf_name = "_thumb_intermediate.pdf"
    libreoffice.to_pdf(input_filename=input_filename, output_filename=pdf_name)
    pdf_path = output_path(pdf_name)
    if not pdf_path.is_file():
        raise RuntimeError(f"LibreOffice failed to produce {pdf_name}")

    # Step 2: PDF → per-page JPGs via pdftoppm
    workdir = output_path(".")
    out_stem = output_prefix
    cmd = [
        pdftoppm, "-jpeg",
        "-r", str(dpi),
        "-jpegopt", f"quality={quality}",
        str(pdf_path),
        out_stem,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(workdir))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pdftoppm timed out: {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(f"pdftoppm failed (exit {proc.returncode}): {proc.stderr.strip()}")

    # pdftoppm produces <prefix>-1.jpg, <prefix>-2.jpg, ... (1-based)
    thumbnails = []
    total_size = 0
    for entry in sorted(workdir.iterdir()):
        if not entry.is_file():
            continue
        if not entry.name.startswith(f"{out_stem}-") or not entry.name.endswith(".jpg"):
            continue
        # Extract page number for stable ordering
        try:
            stem = entry.name[len(f"{out_stem}-"):-len(".jpg")]
            page_num = int(stem)
        except ValueError:
            continue
        # Rename to zero-padded form for nicer artifact names
        new_name = f"{out_stem}-{page_num:02d}.jpg"
        if new_name != entry.name:
            new_path = entry.with_name(new_name)
            entry.rename(new_path)
            entry = new_path
        size = entry.stat().st_size
        total_size += size
        thumbnails.append({
            "slide_index": page_num,
            "filename": entry.name,
            "size_bytes": size,
        })

    thumbnails.sort(key=lambda t: t["slide_index"])

    # Clean up the intermediate PDF
    try:
        pdf_path.unlink()
    except FileNotFoundError:
        pass

    return {
        "slide_count": len(thumbnails),
        "thumbnails": thumbnails,
        "size_bytes_total": total_size,
        "dpi": dpi,
    }
