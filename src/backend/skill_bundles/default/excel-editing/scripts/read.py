#!/usr/bin/env python3
"""read.py — read-only inspection of a .xlsx workbook.

Three modes (mutually exclusive):

    --mode summary     工作簿概览（sheet 列表、维度、表头、样例行）
    --mode sheet       单 sheet 内的单元格值（可指定 cell_range / max_rows）
    --mode validate    公式静态校验（#REF!、跨表引用、命名区域等）

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage examples:
    read.py --mode summary --input /workspace/wb.xlsx
    read.py --mode summary --input /workspace/wb.xlsx --sample-rows 10
    read.py --mode sheet   --input /workspace/wb.xlsx --sheet "Q3 Revenue"
    read.py --mode sheet   --input /workspace/wb.xlsx --sheet "Q3" --range A1:D20 --max-rows 500
    read.py --mode validate --input /workspace/wb.xlsx
    read.py --mode validate --input /workspace/wb.xlsx --sheet-filter "Model"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    staged_workdir,
)


def cmd_summary(input_path: str, sample_rows: int) -> None:
    from engine.reader import get_summary  # type: ignore

    with staged_workdir({"input.xlsx": input_path}):
        result = get_summary(input_filename="input.xlsx", sample_rows=sample_rows)
    emit_json({"ok": True, "meta": result})


def cmd_sheet(input_path: str, sheet: str, cell_range: str | None, max_rows: int) -> None:
    from engine.reader import get_sheet_data  # type: ignore

    with staged_workdir({"input.xlsx": input_path}):
        result = get_sheet_data(
            input_filename="input.xlsx",
            sheet=sheet,
            cell_range=cell_range,
            max_rows=max_rows,
        )
    emit_json({"ok": True, "meta": result})


def cmd_validate(input_path: str, sheet_filter: str | None) -> None:
    from engine.validator import validate_formulas  # type: ignore

    with staged_workdir({"input.xlsx": input_path}):
        result = validate_formulas(
            input_filename="input.xlsx", sheet_filter=sheet_filter
        )
    emit_json({"ok": True, "meta": result})


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--mode", required=True, choices=["summary", "sheet", "validate"])
    p.add_argument("--input", required=True, help="path to .xlsx file")
    # summary
    p.add_argument(
        "--sample-rows",
        type=int,
        default=5,
        help="[summary] rows per sheet to include in sample (default 5, max 50)",
    )
    # sheet
    p.add_argument("--sheet", help="[sheet|validate] sheet name to operate on")
    p.add_argument(
        "--range",
        dest="cell_range",
        help="[sheet] openpyxl-style range, e.g. 'A1:D20' (omit for whole sheet)",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=1000,
        help="[sheet] hard cap on rows returned (default 1000)",
    )
    # validate
    p.add_argument(
        "--sheet-filter",
        help="[validate] only check this sheet (omit for all sheets)",
    )
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    try:
        if args.mode == "summary":
            cmd_summary(args.input, args.sample_rows)
        elif args.mode == "sheet":
            if not args.sheet:
                emit_error(
                    "ValueError",
                    "--sheet is required for --mode sheet",
                    exit_code=2,
                )
            cmd_sheet(args.input, args.sheet, args.cell_range, args.max_rows)
        elif args.mode == "validate":
            # sheet_filter accepts either --sheet-filter or --sheet (both work)
            sf = args.sheet_filter or args.sheet
            cmd_validate(args.input, sf)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
