"""Parse PPTX files into markdown."""
from __future__ import annotations
import io

def open_pptx(file_bytes: bytes):
    """Open a Presentation from bytes; raise RuntimeError with a Chinese
    install hint if python-pptx is missing."""
    try:
        from pptx import Presentation
    except ImportError:
        raise RuntimeError(
            "python-pptx 未安装，无法解析 PPTX 文件（pip install python-pptx）"
        )
    return Presentation(io.BytesIO(file_bytes))


def extract_slide_text(slide) -> str:
    # Iterate in author/Z order so the rendered output mirrors what a
    # reader sees (title → body → footers), since python-pptx exposes
    # shapes in that order on the slide.shapes collection.
    parts: list[str] = []
    for shape in slide.shapes:
        if not getattr(shape, "has_text_frame", False):
            continue
        try:
            text = shape.text_frame.text
        except Exception:
            continue
        text = (text or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def parse_pptx(file_bytes: bytes) -> str:
    """Parse PPTX bytes → markdown with one ``## Slide N`` section per slide."""
    p = open_pptx(file_bytes)
    sections: list[str] = []
    for idx, slide in enumerate(p.slides):
        body = extract_slide_text(slide) or "(此页无文本)"
        sections.append(f"## Slide {idx + 1}\n\n{body}")
    if not sections:
        raise RuntimeError("PPTX 文件无幻灯片")
    return "\n\n".join(sections)


def parse_pptx_slide_count(file_bytes: bytes) -> int:
    """Cheap probe: return the slide count without extracting any text."""
    return len(open_pptx(file_bytes).slides)


def parse_pptx_slide(file_bytes: bytes, slide_index: int) -> str:
    """Return only the named slide's text (0-indexed). Raises RuntimeError
    on out-of-range index."""
    slides = open_pptx(file_bytes).slides
    total = len(slides)
    if total == 0:
        raise RuntimeError("PPTX 文件无幻灯片")
    if slide_index < 0 or slide_index >= total:
        raise RuntimeError(
            f"slide_index 越界: {slide_index}（共 {total} 页，可选 0..{total - 1}）"
        )
    return extract_slide_text(slides[slide_index]) or "(此页无文本)"
