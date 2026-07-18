"""Formula-first, role-styled workbook builder (ported from minimax-xlsx).

Wraps ``_xlsx_engine.cmd_create`` (the skill's ``xlsx_cli.py create``): builds
a byte-true .xlsx from a workbook spec using the bundled minimal_xlsx template
(13 pre-built cell roles — input/formula/xref/header/currency/pct/int/year/
highlight). Every ``{"formula": "..."}`` cell is emitted as a live ``<f>``
(leading ``=`` stripped) — never a hardcoded value.

Use this for financial / analytical models with cross-sheet references and
role styling. For a plain data dump prefer ``engine.builder``.

Workbook spec:
    {"sheets": [
        {"name": str, "freeze_header"?: bool,
         "columns"?: [{"width": float}],
         "rows": [{"role"?: str, "height"?: float,
                   "cells": [ <primitive>
                            | {"value": <primitive>, "role"?: str}
                            | {"formula": str, "role"?: str} ]}]}]}

Public: build_model(spec, output_filename) -> dict
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ._handle import output_path
from . import _xlsx_engine

_TEMPLATE_DIR = Path(__file__).parent / "templates" / "minimal_xlsx"


class XlsxModelError(RuntimeError):
    """Raised when model workbook creation fails."""


def build_model(*, spec: dict[str, Any], output_filename: str) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise XlsxModelError("spec must be an object")
    sheets = spec.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise XlsxModelError("spec.sheets must be a non-empty list")

    out = output_path(output_filename)
    out.parent.mkdir(parents=True, exist_ok=True)

    ns = SimpleNamespace(
        output=str(out),
        template=str(_TEMPLATE_DIR),
        workbook_json=None,
    )
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _xlsx_engine.cmd_create(ns, {"workbook": spec})
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise XlsxModelError(str(e)) from e

    if rc != 0 or not out.is_file():
        raise XlsxModelError(
            f"model create failed (rc={rc}): {buf.getvalue().strip()[-400:]}"
        )

    return {
        "output_filename": output_filename,
        "sheets": [s.get("name") for s in sheets if isinstance(s, dict)],
        "size_bytes": out.stat().st_size,
    }
