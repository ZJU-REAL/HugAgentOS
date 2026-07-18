"""Split a PDF into multiple files by page ranges (pypdf)."""
from __future__ import annotations

from typing import Any

from ._handle import input_path, output_path


def _parse_range(spec: str, total_pages: int) -> list[int]:
    """Parse a 1-based range spec like ``"1-3"``, ``"5"``, ``"7-9"`` to a 0-based list."""
    spec = spec.strip()
    if "-" in spec:
        a_str, b_str = spec.split("-", 1)
        a = int(a_str.strip())
        b = int(b_str.strip())
    else:
        a = b = int(spec)
    if not (1 <= a <= b <= total_pages):
        raise ValueError(
            f"page range {spec!r} invalid for PDF with {total_pages} pages"
        )
    return list(range(a - 1, b))  # 0-based inclusive


def split(
    *,
    input_filename: str,
    page_ranges: list[str],
    output_filenames: list[str] | None = None,
) -> dict[str, Any]:
    """Produce one output PDF per page range.

    Args:
        input_filename: source PDF in cwd
        page_ranges: list of range specs, e.g. ``["1-3", "4-6", "7"]``
        output_filenames: optional list of output filenames; must match
            ``page_ranges`` in length. If omitted, generated as
            ``part_{i+1}.pdf`` for each range.

    Returns:
        ``{"input_pages", "output_count",
            "outputs": [{"filename", "page_range", "page_count", "size_bytes"}, ...]}``
    """
    from pypdf import PdfReader, PdfWriter

    if not page_ranges:
        raise ValueError("'page_ranges' must contain at least one entry")

    if output_filenames is not None:
        if len(output_filenames) != len(page_ranges):
            raise ValueError(
                f"output_filenames length {len(output_filenames)} does not match "
                f"page_ranges length {len(page_ranges)}"
            )
    else:
        output_filenames = [f"part_{i+1}.pdf" for i in range(len(page_ranges))]

    reader = PdfReader(str(input_path(input_filename)))
    total = len(reader.pages)

    outputs: list[dict[str, Any]] = []
    for spec, fname in zip(page_ranges, output_filenames):
        if not fname.lower().endswith(".pdf"):
            fname = fname + ".pdf"
        indices = _parse_range(spec, total)

        writer = PdfWriter()
        for idx in indices:
            writer.add_page(reader.pages[idx])

        out = output_path(fname)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as fh:
            writer.write(fh)

        outputs.append({
            "filename": fname,
            "page_range": spec,
            "page_count": len(indices),
            "size_bytes": out.stat().st_size,
        })

    return {
        "input_pages": total,
        "output_count": len(outputs),
        "outputs": outputs,
    }
