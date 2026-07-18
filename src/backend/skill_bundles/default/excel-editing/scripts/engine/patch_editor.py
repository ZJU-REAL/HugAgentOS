"""Byte-preserving patch editor for .xlsx (ported from the minimax-xlsx skill).

Wraps ``_xlsx_engine.cmd_edit`` (the skill's ``xlsx_cli.py edit``): unpack →
apply ordered patches → repack, with NO openpyxl round-trip — VBA, pivot
tables, sparklines and conditional formatting survive untouched. Formula cells
stay formulas ("formula-first"); insert/delete-row and rename-sheet auto-rewrite
``<f>`` references across the workbook.

Supported ops (patches[*].op): set_cell, fix_formula, replace_text,
insert_row, add_column, rename_sheet, delete_row.

Public: apply_patches(input_filename, output_filename, patches) -> dict
"""
from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace
from typing import Any

from ._handle import input_path, output_path
from . import _xlsx_engine


class XlsxPatchError(RuntimeError):
    """Raised when patch application fails."""


def apply_patches(*, input_filename: str, output_filename: str,
                   patches: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(patches, list) or not patches:
        raise XlsxPatchError("patches must be a non-empty list")

    src = input_path(input_filename)            # raises FileNotFoundError if absent
    out = output_path(output_filename)
    out.parent.mkdir(parents=True, exist_ok=True)

    ns = SimpleNamespace(
        input=str(src),
        output=str(out),
        patches_json=None,
    )
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _xlsx_engine.cmd_edit(ns, {"patches": patches})
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise XlsxPatchError(str(e)) from e

    if rc != 0 or not out.is_file():
        raise XlsxPatchError(
            f"patch edit failed (rc={rc}): {buf.getvalue().strip()[-400:]}"
        )

    return {
        "output_filename": output_filename,
        "patches_applied": len(patches),
        "size_bytes": out.stat().st_size,
        "ops": [p.get("op") for p in patches if isinstance(p, dict)],
    }
