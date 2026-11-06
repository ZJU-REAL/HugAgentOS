"""Checks for the design and diagram skills shipped in the marketplace.

Five of them are pinned imports of upstream repos; these tests guard the import contract
(licence and provenance shipped, paths rewritten to {baseDir}, offline fonts by default)
and run the stdlib-only scripts the way the sandbox would. Rendering (Playwright, fonts)
is exercised in the sandbox image, not here.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from extensions.paths import SKILLS_DIR

MARKET = SKILLS_DIR / "marketplace"
IMPORTED = ["diagram-design", "huashu-design", "frontend-design", "canvas-design", "theme-factory",
            "ui-ux-pro-max"]
DESIGN_SKILLS = IMPORTED + ["enterprise-brand-guidelines"]


@pytest.mark.parametrize("slug", DESIGN_SKILLS)
def test_design_skill_manifest_and_frontmatter(slug):
    from core.capabilities.skills.registry import split_frontmatter
    from features.marketplace import skill_metadata, skills_state

    manifest = skill_metadata.read_manifest(slug)
    assert manifest and manifest["entry_name"] == slug
    assert manifest["category"] in skills_state.MARKETPLACE_CATEGORIES
    meta, body = split_frontmatter((MARKET / slug / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == slug and meta["description"]
    # The selector matches Chinese requests against the description, so it must carry Chinese.
    assert re.search(r"[一-鿿]", meta["description"])


@pytest.mark.parametrize("slug", IMPORTED)
def test_imported_skill_ships_licence_and_pinned_provenance(slug):
    root = MARKET / slug
    assert (root / "LICENSE").is_file() or (root / "LICENSE.txt").is_file()
    upstream = (root / "UPSTREAM.md").read_text(encoding="utf-8")
    assert "固定 commit" in upstream and "本地适配" in upstream
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    for leftover in ("${CLAUDE_PLUGIN_ROOT}", '"$HD/', "git -C <本目录> pull"):
        assert leftover not in skill


def test_diagram_design_defaults_to_offline_fonts():
    guide = (MARKET / "diagram-design/references/style-guide.md").read_text(encoding="utf-8")
    assert "Font source: `system`" in guide
    assert "'WenQuanYi Zen Hei', 'FZHei-B01S'" in guide


def test_huashu_ships_without_video_and_cloud_pipeline():
    root = MARKET / "huashu-design"
    assert not list(root.glob("assets/bgm-*.mp3"))
    assert not (root / "scripts/cloud").exists()
    pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    # Playwright comes pinned with the sandbox; a second copy would fight it.
    assert "playwright" not in pkg["dependencies"]
    for script in ("export_deck_pdf.mjs", "export_deck_pptx.mjs", "gen_deck_thumbs.mjs",
                   "export_deck_stage_pdf.mjs"):
        text = (root / "scripts" / script).read_text(encoding="utf-8")
        assert "createRequire(import.meta.url)" in text
        assert "from 'playwright'" not in text and "from 'pptxgenjs'" not in text


def test_diagram_design_self_check_passes_on_a_shipped_example(tmp_path):
    root = MARKET / "diagram-design"
    proc = subprocess.run([sys.executable, str(root / "scripts/self_check.py"),
                           str(root / "assets/example-architecture.html")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_ui_ux_search_answers_an_english_query():
    script = MARKET / "ui-ux-pro-max/scripts/search.py"
    proc = subprocess.run([sys.executable, str(script), "government public service mini program",
                           "--design-system", "-p", "Gov", "--json"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "government" in proc.stdout.lower()


def _brand(tmp_path, spec: dict):
    path = tmp_path / "brand.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return subprocess.run([sys.executable, str(MARKET / "enterprise-brand-guidelines/scripts/brand_tokens.py"),
                           str(path), "-o", str(tmp_path / "out")],
                          capture_output=True, text=True, timeout=60)


def test_brand_tokens_replace_unreadable_text_colours(tmp_path):
    sys.path.insert(0, str(MARKET / "enterprise-brand-guidelines/scripts"))
    try:
        import brand_tokens
    finally:
        sys.path.pop(0)
    template = json.loads((MARKET / "enterprise-brand-guidelines/templates/brand.json").read_text("utf-8"))
    proc = _brand(tmp_path, template)
    assert proc.returncode == 0, proc.stderr
    tokens = json.loads((tmp_path / "out/tokens.json").read_text(encoding="utf-8"))
    for row in tokens["colors"]:
        # The replacement must clear AA as text, and a colour that already did is left untouched.
        assert brand_tokens.contrast(row["text_variant"], template["colors"]["paper"]) >= 4.5
        if row["ok_as_text"]:
            assert row["text_variant"] == row["color"]
    accent = next(r for r in tokens["colors"] if r["role"] == "accent")
    assert not accent["ok_as_text"] and accent["text_on_fill"] == template["colors"]["ink"]
    css = (tmp_path / "out/tokens.css").read_text(encoding="utf-8")
    assert f"--brand-accent-text: {accent['text_variant']};" in css
    assert "'Microsoft YaHei'" in css and "sans-serif;" in css and "'sans-serif'" not in css
    assert (tmp_path / "out/theme.md").read_text("utf-8").startswith("# 示例集团")


def test_brand_tokens_reject_an_incomplete_spec(tmp_path):
    proc = _brand(tmp_path, {"name": "x", "colors": {"primary": "red", "ink": "#000000"}})
    assert proc.returncode == 2
    errors = json.loads(proc.stdout)["errors"]
    assert "colors 缺少必填项 paper" in errors and any("primary" in e for e in errors)
