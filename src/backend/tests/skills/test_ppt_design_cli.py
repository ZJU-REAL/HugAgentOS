"""Smoke tests for the ppt-design skill CLI (``skill_bundles/default/ppt-design/
scripts/ppt.py``).

Verifies the CLI surface end-to-end: build (both engines), info, extract,
check-placeholders, add-slide, delete-slide, to-pdf, and the argparse
validation paths (invalid theme, etc.). The engine itself is exercised
transitively — there is no separate engine-level pytest suite.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]  # src/backend
SKILL_DIR = BACKEND_ROOT / "skill_bundles/default/ppt-design"
CLI = SKILL_DIR / "scripts/ppt.py"
SKELETON = SKILL_DIR / "assets/skeleton-default.json"


def _run(*args: str, cwd: Path | None = None) -> dict:
    """Run the CLI; return parsed JSON from stdout. Raise on non-zero exit."""
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ppt.py {args} exited {proc.returncode}\n"
            f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
        )
    if not proc.stdout.strip():
        raise RuntimeError(f"ppt.py {args} produced no stdout; stderr={proc.stderr!r}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _pptxgenjs_available() -> bool:
    """True iff node + the pptxgenjs npm package are both reachable."""
    if shutil.which("node") is None:
        return False
    try:
        proc = subprocess.run(
            ["node", "-e", "require('pptxgenjs'); process.exit(0)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _libreoffice_available() -> bool:
    return shutil.which("libreoffice") is not None or shutil.which("soffice") is not None


def test_cli_smoke_assets_present():
    assert CLI.is_file(), f"CLI not found at {CLI}"
    assert SKELETON.is_file(), f"skeleton fixture missing at {SKELETON}"


def test_list_introspection_commands():
    """list-themes / list-styles / list-slide-types should all return the expected sets.

    Core themes are ``swiss_klein`` (default / general) and ``navy_gold`` (tech).
    The catalog was extended with a gallery of family themes distilled from
    open-source decks (see references/palette-gallery.md), each paired with a
    design pack — so the palette list is now a superset of the two core themes.
    """
    themes = _run("list-themes")
    assert themes["ok"] is True
    palettes = set(themes["palettes"])
    assert {"swiss_klein", "navy_gold"} <= palettes
    # extended catalog: a representative family theme from each tier is present
    assert {"swiss_grid", "ink_chinese", "glass_dashboard", "blueprint"} <= palettes
    assert len(palettes) >= 20
    assert isinstance(themes["aliases"], dict) and len(themes["aliases"]) >= 2

    styles = _run("list-styles")
    assert styles["styles"] == ["sharp", "soft", "rounded", "pill"]

    types = _run("list-slide-types")
    assert set(types["slide_types"]) == {"cover", "toc", "section", "content", "summary"}


@pytest.fixture(scope="session")
def baseline_deck(tmp_path_factory) -> Path:
    """Build the skeleton-data-report deck once per pytest session.

    Most tests below only need a valid .pptx to inspect / mutate; they don't
    each need a fresh build. The fixture writes the deck into a session-scoped
    temp dir and returns its path. Mutating tests should ``shutil.copyfile``
    it into their own ``tmp_path`` before exercising edit commands.
    """
    out = tmp_path_factory.mktemp("baseline") / "deck.pptx"
    _run(
        "build",
        "--spec", str(SKELETON),
        "--output", str(out),
        "--engine", "python-pptx",
        "--style", "soft",
    )
    return out


def _stage(baseline: Path, tmp_path: Path) -> Path:
    """Copy the baseline deck into a per-test path the test owns and may mutate."""
    out = tmp_path / "deck.pptx"
    shutil.copyfile(baseline, out)
    return out


def test_build_python_pptx_engine(baseline_deck):
    """The baseline_deck fixture exercises this code path; verify what it produced."""
    assert baseline_deck.is_file() and baseline_deck.stat().st_size > 5_000
    # Cross-check via info — confirms the fixture's .pptx is readable + multi-slide.
    info = _run("info", str(baseline_deck))
    assert info["ok"] is True
    assert info["slide_count"] >= 5


@pytest.mark.skipif(not _pptxgenjs_available(), reason="Node + pptxgenjs not available")
def test_build_pptxgenjs_engine(tmp_path):
    """Same skeleton, pptxgenjs engine — needs Node + npm install -g pptxgenjs."""
    out = tmp_path / "deck.pptx"
    result = _run(
        "build",
        "--spec", str(SKELETON),
        "--output", str(out),
        "--engine", "pptxgenjs",
        "--style", "soft",
    )
    assert result["ok"] is True
    assert out.is_file() and out.stat().st_size > 10_000  # pptxgenjs decks are heavier
    assert result["meta"]["engine"] == "pptxgenjs"


def test_info_and_extract_round_trip(baseline_deck):
    info = _run("info", str(baseline_deck))
    assert info["ok"] is True
    n = info["slide_count"]
    assert n >= 5
    assert len(info["slides"]) == n
    assert all("index" in s and "title" in s for s in info["slides"])

    count = _run("slide-count", str(baseline_deck))
    assert count["slide_count"] == n

    # full extract
    all_text = _run("extract", str(baseline_deck))
    assert all_text["block_count"] > 0
    assert isinstance(all_text["text_blocks"], list)

    # single-slide extract
    one = _run("extract", str(baseline_deck), "--slide", "0")
    assert one["slide_index"] == 0
    assert "joined_text" in one


def test_check_placeholders_detects_xxxx(tmp_path):
    """A deck containing placeholder literals (xxxx / 待补充) must be flagged."""
    spec = tmp_path / "placeholder.json"
    spec.write_text(json.dumps({
        "title": "XXXX 测试",
        "slides": [
            {"type": "cover", "title": "XXXX 年度经营分析报告"},
            {"type": "content", "title": "概览", "bullets": ["待补充", "正常要点"]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    deck = tmp_path / "deck.pptx"
    _run("build", "--spec", str(spec), "--output", str(deck), "--engine", "python-pptx")

    result = _run("check-placeholders", str(deck))
    assert result["ok"] is True
    assert result["is_clean"] is False
    assert result["hit_count"] >= 1


def test_delete_slide(baseline_deck, tmp_path):
    out = _stage(baseline_deck, tmp_path)
    before = _run("slide-count", str(out))

    smaller = tmp_path / "trimmed.pptx"
    result = _run("delete-slide", str(out), "--slide", "0", "--output", str(smaller))
    assert result["ok"] is True
    assert smaller.is_file()

    after = _run("slide-count", str(smaller))
    assert after["slide_count"] == before["slide_count"] - 1


@pytest.mark.skipif(not _libreoffice_available(), reason="LibreOffice headless not in PATH")
def test_to_pdf(baseline_deck, tmp_path):
    pdf = tmp_path / "deck.pdf"
    result = _run("to-pdf", str(baseline_deck), "--output", str(pdf))
    assert result["ok"] is True
    assert pdf.is_file() and pdf.stat().st_size > 1_000


def test_add_slide_summary_with_content_string(baseline_deck, tmp_path):
    """Regression for the eval-discovered bug: --content as plain string used to
    crash the engine ('str' has no attribute 'get'). The CLI now splits it into
    {bullets: [...]} for content/summary slides.
    """
    out = _stage(baseline_deck, tmp_path)

    extended = tmp_path / "extended.pptx"
    result = _run(
        "add-slide", str(out),
        "--output", str(extended),
        "--type", "summary",
        "--title", "下一步行动",
        "--content", "优先推进试点\n3 个月内完成 2 个 region 落地",
    )
    assert result["ok"] is True
    assert extended.is_file() and extended.stat().st_size > out.stat().st_size

    # the new last slide should contain the body bullet text
    info = _run("info", str(extended))
    last_idx = info["slide_count"] - 1
    last = _run("extract", str(extended), "--slide", str(last_idx))
    joined = last["joined_text"]
    assert "下一步行动" in joined
    assert "优先推进试点" in joined
    assert "region" in joined


def test_invalid_theme_rejected(tmp_path):
    """Bad --theme should exit non-zero with a ValueError on stderr."""
    out = tmp_path / "deck.pptx"
    proc = subprocess.run(
        [
            sys.executable, str(CLI), "build",
            "--spec", str(SKELETON), "--output", str(out),
            "--engine", "python-pptx", "--theme", "this-palette-does-not-exist",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert err["ok"] is False
    assert err["error"]["type"] == "ValueError"
    assert "theme" in err["error"].get("field", "")
