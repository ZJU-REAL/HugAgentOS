"""pdf-editing skill engine — print-quality PDF generation (ported minimax-pdf).

Exercises the pipeline without Chromium: the cover degrades to the pure
reportlab fallback, so this validates palette + body (table/chart/math/
flowchart) + merge + reformat end to end. Skips if reportlab/pypdf absent.
"""
import importlib.util
import tempfile
from pathlib import Path

import pytest

from tests._skill_engine import load_engine

load_engine("pdf-editing", "pdf_engine")

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("reportlab") is None
    or importlib.util.find_spec("pypdf") is None,
    reason="reportlab/pypdf only installed in the mcp container",
)


def _read_pages(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def test_pdf_create_full_pipeline():
    from pdf_engine._handle import use_workdir
    from pdf_engine import creator

    spec = {
        "title": "能力评估报告",
        "doc_type": "report",
        "author": "技术组",
        "date": "2026-05",
        "subtitle": "PDF 精排生成验证",
        "content": [
            {"type": "h1", "text": "概述"},
            {"type": "body", "text": "验证 <b>pdf-editing</b> 精排生成。"},
            {"type": "bullet", "text": "封面兜底"},
            {"type": "callout", "text": "结论：渲染链路完整。"},
            {"type": "table", "headers": ["指标", "值"],
             "rows": [["提交", "30"], ["缺陷", "1"]], "caption": "表 1"},
            {"type": "chart", "chart_type": "bar", "labels": ["A", "B"],
             "datasets": [{"label": "x", "values": [3, 7]}]},
            {"type": "math", "text": "E = mc^2", "label": "(1)"},
            {"type": "flowchart",
             "nodes": [{"id": "s", "label": "start", "shape": "oval"},
                       {"id": "e", "label": "end", "shape": "oval"}],
             "edges": [{"from": "s", "to": "e"}]},
            {"type": "pagebreak"},
            {"type": "h1", "text": "结论"},
            {"type": "body", "text": "通过。"},
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        with use_workdir(tmp):
            res = creator.create(spec=spec, output_filename="out.pdf")
        out = Path(tmp) / "out.pdf"
        assert res["output_filename"] == "out.pdf"
        assert out.is_file() and out.stat().st_size > 5000
        assert res["pages"] >= 2
        assert _read_pages(out) == res["pages"]
        # No Chromium in CI → fallback path must keep pdf_create alive.
        assert res["cover_mode"] in ("chromium", "reportlab_fallback")


def test_pdf_reformat_markdown():
    from pdf_engine._handle import use_workdir
    from pdf_engine import creator

    with tempfile.TemporaryDirectory() as tmp:
        with use_workdir(tmp):
            (Path(tmp) / "src.md").write_text(
                "# 重排测试\n\n这是 **markdown**。\n\n- 一\n- 二\n\n## 小节\n\n完。\n",
                encoding="utf-8",
            )
            res = creator.reformat(
                input_filename="src.md", doc_type="report",
                output_filename="rf.pdf",
            )
        rf = Path(tmp) / "rf.pdf"
        assert rf.is_file() and rf.stat().st_size > 3000
        assert res["pages"] >= 1
        assert "reformat_warnings" in res


def test_pdf_create_rejects_bad_spec():
    from pdf_engine import creator

    with pytest.raises(creator.PdfCreateError):
        creator.create(spec={"content": [{"type": "body", "text": "x"}]},
                        output_filename="x.pdf")
    with pytest.raises(creator.PdfCreateError):
        creator.create(spec={"title": "t", "content": []},
                        output_filename="x.pdf")


def test_palette_normalize_hex_accepts_3_and_6_digit():
    """Regression: ``build_tokens(accent_override='#0a5')`` used to crash
    deep inside ``_hex_to_rgb`` with ``int('', 16)`` because the slice
    ``h[4:6]`` is empty for a 3-digit shorthand. CSS-style 3-digit form
    should expand to 6 digits the same way browsers do.
    """
    from pdf_engine._palette import (
        _hex_to_rgb,
        _normalize_hex,
        build_tokens,
    )

    # 3-digit shorthand (CSS) expands per-digit
    assert _normalize_hex("#0a5") == "00aa55"
    assert _normalize_hex("0a5") == "00aa55"
    # 6-digit canonical lowercased
    assert _normalize_hex("#00AA55") == "00aa55"
    # rgb tuple matches expanded form
    assert _hex_to_rgb("#0a5") == _hex_to_rgb("#00aa55") == (0, 170, 85)

    # malformed inputs raise ValueError instead of dying with an opaque
    # int('', 16) error inside the cover renderer
    for bad in ("", "#", "#0a", "#0a55", "#xyz", "rgb(0,170,85)"):
        try:
            _normalize_hex(bad)
        except ValueError:
            continue
        raise AssertionError(f"_normalize_hex({bad!r}) should have raised ValueError")

    # build_tokens with the short form stores the canonical 6-digit string
    t = build_tokens(title="Test", doc_type="report", accent_override="#0a5")
    assert t["accent"].lower() == "#00aa55"
