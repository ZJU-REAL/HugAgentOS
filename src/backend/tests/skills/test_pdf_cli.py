"""Smoke tests for the pdf-editing skill CLI.

Replaces the old ``mcp_servers/_office_shared/_integration_test.py::test_pdf``
(removed when pdf_mcp was deleted). Exercises every subcommand end-to-end:
read (5 modes), merge, split, fill-form, create, reformat. The argparse +
emit_json contract is what changed when migrating from MCP to skill;
the engine itself is covered by ``test_office_pdf_create.py``.

Some tests require pypdf (for synthesizing fixtures or running splitter
internals); they're skipped automatically when pypdf isn't installed locally.
The integration test inside the mcp container runs the full suite.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]  # src/backend
SKILL_DIR = BACKEND_ROOT / "skill_bundles/default/pdf-editing"
CLI = SKILL_DIR / "scripts/cli.py"


def _run(*args: str) -> dict:
    """Run the CLI; return parsed JSON from stdout. Raise on non-zero exit."""
    env = os.environ.copy()
    # The skill is self-contained: cli.py adds its own scripts/ dir to
    # sys.path (via _common.setup_path), so the vendored ``engine`` package
    # imports without any extra PYTHONPATH wiring.
    env["PYTHONPATH"] = (
        str(BACKEND_ROOT)
        + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
    )
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pdf-cli {args} exited {proc.returncode}\n"
            f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
        )
    if not proc.stdout.strip():
        raise RuntimeError(
            f"pdf-cli {args} produced no stdout; stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _pypdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def _make_blank_pdf(path: Path, pages: int = 3) -> None:
    """Build a minimal valid N-page PDF using raw bytes (no pypdf dependency).

    Same trick as _office_shared/_integration_test.py: hand-roll the structure
    so the test fixture works on dev machines that don't have pypdf installed.
    """
    page_objs = []
    for i in range(pages):
        page_ref = 4 + i * 2
        content_ref = page_ref + 1
        page_objs.append((page_ref, content_ref))

    kids = " ".join(f"{ref} 0 R" for ref, _ in page_objs)

    objects: list[tuple[int, bytes]] = []
    objects.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append(
        (
            2,
            f"<< /Type /Pages /Count {pages} /Kids [{kids}] >>".encode("utf-8"),
        )
    )
    objects.append(
        (3, b"<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>")
    )
    for i, (page_ref, content_ref) in enumerate(page_objs):
        objects.append(
            (
                page_ref,
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources 3 0 R /Contents {content_ref} 0 R >>".encode("utf-8"),
            )
        )
        text = f"BT /F1 24 Tf 72 720 Td (Page {i + 1}) Tj ET"
        stream = text.encode("utf-8")
        objects.append(
            (
                content_ref,
                f"<< /Length {len(stream)} >>\nstream\n".encode("utf-8")
                + stream
                + b"\nendstream",
            )
        )

    buf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = []
    for num, body in objects:
        offsets.append((num, len(buf)))
        buf += f"{num} 0 obj\n".encode("utf-8") + body + b"\nendobj\n"

    xref_start = len(buf)
    buf += f"xref\n0 {len(objects) + 1}\n".encode("utf-8")
    buf += b"0000000000 65535 f \n"
    offsets.sort()
    for _, off in offsets:
        buf += f"{off:010d} 00000 n \n".encode("utf-8")
    buf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_start}\n%%EOF"
    ).encode("utf-8")

    path.write_bytes(buf)


def test_cli_files_present():
    assert CLI.is_file(), f"CLI not found at {CLI}"
    for sub in ("read.py", "merge.py", "split.py", "fill_form.py", "create.py", "reformat.py", "_common.py"):
        assert (SKILL_DIR / "scripts" / sub).is_file(), f"missing {sub}"
    assert (SKILL_DIR / "SKILL.md").is_file()


def test_help_lists_subcommands():
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 2
    for sub in ("read", "merge", "split", "fill-form", "create", "reformat"):
        assert sub in proc.stdout


@pytest.mark.skipif(not _pypdf_available(), reason="pypdf not installed")
def test_read_metadata_and_overview(tmp_path):
    pdf = tmp_path / "test.pdf"
    _make_blank_pdf(pdf, pages=3)

    md = _run("read", "--mode", "metadata", "--input", str(pdf))
    assert md["ok"] is True
    assert md["meta"]["page_count"] == 3

    ov = _run("read", "--mode", "overview", "--input", str(pdf))
    assert ov["ok"] is True
    assert ov["meta"]["page_count"] == 3
    # outline likely empty for hand-rolled PDF
    assert "outline" in ov["meta"]


@pytest.mark.skipif(not _pypdf_available(), reason="pypdf not installed")
def test_read_text(tmp_path):
    pdf = tmp_path / "test.pdf"
    _make_blank_pdf(pdf, pages=2)

    r = _run("read", "--mode", "text", "--input", str(pdf))
    assert r["ok"] is True
    assert r["meta"]["page_count"] == 2

    # specific pages
    r2 = _run("read", "--mode", "text", "--input", str(pdf), "--pages", "1")
    assert r2["ok"] is True
    assert r2["meta"]["selected_pages"] == [1]


@pytest.mark.skipif(not _pypdf_available(), reason="pypdf not installed")
def test_merge(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    _make_blank_pdf(a, pages=2)
    _make_blank_pdf(b, pages=3)
    out = tmp_path / "merged.pdf"

    r = _run("merge", "--output", str(out), "--inputs", str(a), str(b))
    assert r["ok"] is True
    assert r["meta"]["total_pages"] == 5
    assert out.is_file()


@pytest.mark.skipif(not _pypdf_available(), reason="pypdf not installed")
def test_split(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_blank_pdf(pdf, pages=5)
    out_dir = tmp_path / "parts"

    r = _run(
        "split", "--input", str(pdf), "--output-dir", str(out_dir),
        "--ranges", "1-2,3-5",
    )
    assert r["ok"] is True
    assert r["meta"]["output_count"] == 2
    for out_info in r["meta"]["outputs"]:
        assert Path(out_info["path"]).is_file()


def test_merge_needs_at_least_2_inputs(tmp_path):
    """Single-input merge should be rejected by argparse."""
    a = tmp_path / "a.pdf"
    a.write_bytes(b"%PDF-1.4\n%%EOF")
    proc = subprocess.run(
        [sys.executable, str(CLI), "merge", "--output", str(tmp_path / "x.pdf"),
         "--inputs", str(a)],
        capture_output=True, text=True, timeout=10,
        env={**os.environ,
             "PYTHONPATH": str(BACKEND_ROOT)},
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False


def test_reformat_unsupported_extension(tmp_path):
    """Reformat rejects extensions outside the allowed set."""
    src = tmp_path / "weird.xyz"
    src.write_text("hello")
    proc = subprocess.run(
        [sys.executable, str(CLI), "reformat",
         "--input", str(src), "--output", str(tmp_path / "out.pdf")],
        capture_output=True, text=True, timeout=10,
        env={**os.environ,
             "PYTHONPATH": str(BACKEND_ROOT)},
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "unsupported" in payload["error"]["message"].lower()
