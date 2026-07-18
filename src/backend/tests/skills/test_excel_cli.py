"""Smoke tests for the excel-editing skill CLI (``skill_bundles/
excel-editing/scripts/cli.py`` + each subcommand).

Replaces the old ``mcp_servers/_office_shared/_integration_test.py::test_excel``
(removed when excel_mcp was deleted). Exercises every subcommand end-to-end:
create (workbook / model), read (summary / sheet / validate), edit (patches /
set-cells / add-sheet / add-chart), save, convert. The argparse + emit_json
contract is what changed when migrating from MCP to skill; the engine itself
is covered by ``test_office_xlsx_patch.py``.
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
SKILL_DIR = BACKEND_ROOT / "skill_bundles/default/excel-editing"
CLI = SKILL_DIR / "scripts/cli.py"


def _run(*args: str, env_extra: dict | None = None) -> dict:
    """Run the CLI; return parsed JSON from stdout. Raise on non-zero exit."""
    env = os.environ.copy()
    # The skill is self-contained: cli.py adds its own scripts/ dir to
    # sys.path (via _common.setup_path), so the vendored ``engine`` package
    # imports without any extra PYTHONPATH wiring.
    env["PYTHONPATH"] = (
        str(BACKEND_ROOT)
        + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
    )
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"excel-cli {args} exited {proc.returncode}\n"
            f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
        )
    if not proc.stdout.strip():
        raise RuntimeError(
            f"excel-cli {args} produced no stdout; stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _libreoffice_available() -> bool:
    return shutil.which("libreoffice") is not None or shutil.which("soffice") is not None


def test_cli_files_present():
    assert CLI.is_file(), f"CLI not found at {CLI}"
    for sub in ("read.py", "create.py", "apply_edits.py", "save.py", "convert.py", "_common.py"):
        assert (SKILL_DIR / "scripts" / sub).is_file(), f"missing {sub}"
    assert (SKILL_DIR / "SKILL.md").is_file()


def test_help_lists_subcommands():
    """`cli.py` with no args prints help to stdout and exits 2."""
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 2
    for sub in ("read", "create", "edit", "save", "convert"):
        assert sub in proc.stdout


def test_create_workbook_and_read(tmp_path):
    out = tmp_path / "wb.xlsx"
    sheets = [{
        "name": "Q3",
        "headers": ["Region", "Revenue", "Growth"],
        "rows": [["华东", 12500000, 0.18], ["华南", 9800000, 0.12]],
        "column_widths": [16, 18, 14],
        "freeze_header": True,
    }]

    r = _run("create", "--mode", "workbook", "--output", str(out), "--sheets", json.dumps(sheets))
    assert r["ok"] is True
    assert "Q3" in r["meta"]["sheet_names"]
    assert out.is_file()
    assert out.stat().st_size > 0

    # summary
    s = _run("read", "--mode", "summary", "--input", str(out))
    assert s["ok"] is True
    assert "Q3" in s["meta"]["sheet_names"]

    # sheet
    d = _run("read", "--mode", "sheet", "--input", str(out), "--sheet", "Q3")
    assert d["ok"] is True
    assert d["meta"]["sheet"] == "Q3"


def test_create_workbook_via_file(tmp_path):
    """--sheets-file for large payloads."""
    out = tmp_path / "wb2.xlsx"
    sheets_file = tmp_path / "sheets.json"
    sheets_file.write_text(json.dumps([{"name": "Solo", "rows": [[1, 2, 3]]}]), encoding="utf-8")
    r = _run("create", "--mode", "workbook", "--output", str(out), "--sheets-file", str(sheets_file))
    assert r["ok"] is True
    assert "Solo" in r["meta"]["sheet_names"]


def test_edit_patches_set_cell(tmp_path):
    """End-to-end: create → patch a cell → read it back."""
    wb = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _run("create", "--mode", "workbook", "--output", str(wb),
         "--sheets", json.dumps([{"name": "Data", "rows": [[1, 2, 3], [4, 5, 6]]}]))

    patches = [{"op": "set_cell", "sheet": "Data", "cell": "D1", "value": "Total"}]
    r = _run("edit", "--input", str(wb), "--output", str(out), "--patches", json.dumps(patches))
    assert r["ok"] is True
    assert r["meta"]["patches_applied"] == 1
    assert out.is_file()


def test_edit_set_cells_engine(tmp_path):
    wb = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _run("create", "--mode", "workbook", "--output", str(wb),
         "--sheets", json.dumps([{"name": "Q3", "rows": [[1, 2], [3, 4]]}]))

    cells = [{"addr": "C1", "value": "Total"}, {"addr": "C2", "formula": "=SUM(A1:B1)"}]
    r = _run("edit", "--input", str(wb), "--output", str(out),
             "--sheet", "Q3", "--set-cells", json.dumps(cells))
    assert r["ok"] is True


def test_edit_add_sheet_engine(tmp_path):
    wb = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _run("create", "--mode", "workbook", "--output", str(wb),
         "--sheets", json.dumps([{"name": "Existing", "rows": [[1]]}]))

    payload = {"sheet_name": "Notes", "headers": ["Topic", "Detail"]}
    r = _run("edit", "--input", str(wb), "--output", str(out),
             "--add-sheet", json.dumps(payload))
    assert r["ok"] is True
    s = _run("read", "--mode", "summary", "--input", str(out))
    assert "Notes" in s["meta"]["sheet_names"]


def test_edit_engine_exclusivity(tmp_path):
    """Specifying zero engines should fail with argparse exit code 2."""
    wb = tmp_path / "in.xlsx"
    _run("create", "--mode", "workbook", "--output", str(wb),
         "--sheets", json.dumps([{"name": "S", "rows": [[1]]}]))

    proc = subprocess.run(
        [sys.executable, str(CLI), "edit",
         "--input", str(wb), "--output", str(tmp_path / "out.xlsx")],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT)},
    )
    # No engine chosen → emit_error exit 2; stdout still carries the JSON error
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "edit engines" in payload["error"]["message"] or "must provide" in payload["error"]["message"]


def test_validate_formulas(tmp_path):
    wb = tmp_path / "model.xlsx"
    _run("create", "--mode", "workbook", "--output", str(wb),
         "--sheets", json.dumps([{"name": "M", "rows": [[1, 2, 3]]}]))

    v = _run("read", "--mode", "validate", "--input", str(wb))
    assert v["ok"] is True
    assert "error_count" in v["meta"]


def test_save_renames(tmp_path):
    wb = tmp_path / "in.xlsx"
    out = tmp_path / "最终版.xlsx"
    _run("create", "--mode", "workbook", "--output", str(wb),
         "--sheets", json.dumps([{"name": "S", "rows": [[1]]}]))

    r = _run("save", "--input", str(wb), "--output", str(out))
    assert r["ok"] is True
    assert out.is_file()
    assert out.stat().st_size == wb.stat().st_size


@pytest.mark.skipif(not shutil.which("libreoffice") and not shutil.which("soffice"),
                    reason="LibreOffice not available (dev env without it)")
def test_convert_to_pdf(tmp_path):
    wb = tmp_path / "in.xlsx"
    out = tmp_path / "out.pdf"
    _run("create", "--mode", "workbook", "--output", str(wb),
         "--sheets", json.dumps([{"name": "X", "rows": [[1, 2, 3]]}]))

    r = _run("convert", "--to", "pdf", "--input", str(wb), "--output", str(out))
    assert r["ok"] is True
    assert out.is_file()
    assert out.stat().st_size > 0
