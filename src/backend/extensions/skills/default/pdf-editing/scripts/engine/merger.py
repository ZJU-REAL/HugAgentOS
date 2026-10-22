"""PDF merging — concatenate multiple PDFs into one.

Modeled on ``agent_skills/skills/minimax-pdf/scripts/merge.py`` but uses pypdf
directly (the original skill script defensively pip-installed pypdf at runtime;
the sandbox image now ships it pre-installed, so we just import).
"""
from __future__ import annotations

from typing import Any

from ._handle import input_path, output_path


def merge(
    *,
    input_filenames: list[str],
    output_filename: str,
) -> dict[str, Any]:
    """Concatenate PDFs in the given order into a single output PDF.

    Args:
        input_filenames: list of PDF filenames in cwd, in concatenation order
        output_filename: destination PDF filename

    Returns:
        ``{"output_filename", "input_count", "total_pages", "per_input": [...]}``
    """
    from pypdf import PdfReader, PdfWriter

    if not input_filenames:
        raise ValueError("'input_filenames' must contain at least one entry")

    writer = PdfWriter()
    per_input: list[dict[str, Any]] = []
    total_pages = 0

    for fname in input_filenames:
        src = str(input_path(fname))
        reader = PdfReader(src)
        n = len(reader.pages)
        for page in reader.pages:
            writer.add_page(page)
        per_input.append({"filename": fname, "page_count": n})
        total_pages += n

    out = output_path(output_filename)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        writer.write(fh)

    return {
        "output_filename": output_filename,
        "input_count": len(input_filenames),
        "total_pages": total_pages,
        "per_input": per_input,
        "size_bytes": out.stat().st_size,
    }
