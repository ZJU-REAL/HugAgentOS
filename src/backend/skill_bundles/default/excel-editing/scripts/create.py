#!/usr/bin/env python3
"""create.py — generate a new .xlsx from scratch.

Two modes:

    --mode workbook    plain data dump (sheet name + headers + rows + column widths)
                       → engine.builder.create_workbook
    --mode model       formula-first financial / analytical model with role styling
                       (input / formula / xref / header / pct / currency / int / …)
                       → engine.model_builder.build_model

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage examples:
    create.py --mode workbook --output /workspace/out.xlsx --sheets '[{"name":"Q3","headers":["地区","收入"],"rows":[["华东",125]]}]'
    create.py --mode workbook --output /workspace/out.xlsx --sheets-file /workspace/sheets.json
    create.py --mode model    --output /workspace/model.xlsx --spec-file /workspace/spec.json

For large payloads (multi-sheet dumps, 100+ row models) prefer the *-file
variant: write the JSON to /workspace/<name>.json first, then pass it via
--sheets-file / --spec-file. This avoids ``Argument list too long`` in bash.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    load_json_arg_or_file,
    staged_workdir,
)


def _ensure_xlsx(name: str) -> str:
    return name if name.endswith(".xlsx") else name + ".xlsx"


def cmd_workbook(output_path: str, sheets: list[dict] | None) -> None:
    from engine.builder import create_workbook  # type: ignore

    out_basename = "out.xlsx"
    with staged_workdir({}, output_name=out_basename, output_dst=output_path):
        result = create_workbook(filename=out_basename, sheets=sheets)
    emit_json({"ok": True, "meta": result})


def cmd_model(output_path: str, spec: dict) -> None:
    from engine.model_builder import build_model  # type: ignore

    if not isinstance(spec, dict) or not isinstance(spec.get("sheets"), list) \
            or not spec.get("sheets"):
        emit_error(
            "ValueError",
            "spec.sheets must be a non-empty list",
            exit_code=2,
        )

    out_basename = "out.xlsx"
    with staged_workdir({}, output_name=out_basename, output_dst=output_path):
        result = build_model(spec=spec, output_filename=out_basename)
    emit_json({"ok": True, "meta": result})


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--mode", required=True, choices=["workbook", "model"])
    p.add_argument(
        "--output",
        required=True,
        help="absolute path to write the .xlsx into (e.g. /workspace/out.xlsx)",
    )
    # workbook mode
    p.add_argument(
        "--sheets",
        help='[workbook] JSON list of sheet specs (see docstring); omit for a single empty "Sheet1"',
    )
    p.add_argument(
        "--sheets-file",
        help="[workbook] path to a JSON file containing the sheets list (use for large payloads)",
    )
    # model mode
    p.add_argument(
        "--spec",
        help="[model] JSON model spec object (top-level keys: sheets)",
    )
    p.add_argument(
        "--spec-file",
        help="[model] path to JSON file containing the model spec (use for large payloads)",
    )
    args = p.parse_args()

    output_path = _ensure_xlsx(args.output)

    try:
        if args.mode == "workbook":
            if args.sheets is None and args.sheets_file is None:
                # both omitted is fine — create a default empty Sheet1
                sheets = None
            else:
                sheets = load_json_arg_or_file(args.sheets, args.sheets_file, "sheets")
                if sheets is not None and not isinstance(sheets, list):
                    emit_error(
                        "ValueError",
                        "--sheets must decode to a JSON array",
                        exit_code=2,
                    )
            cmd_workbook(output_path, sheets)
        elif args.mode == "model":
            spec = load_json_arg_or_file(args.spec, args.spec_file, "spec")
            cmd_model(output_path, spec)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
