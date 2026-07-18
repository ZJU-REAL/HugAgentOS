#!/usr/bin/env python3
"""apply_edits.py — edit an existing .xlsx workbook.

Exactly one of the following edit engines must be selected per call. They are
NOT composable (each uses a different write path), but all five share the same
input/output staging:

    --patches '[{op:..., ...}, ...]'
        Byte-preserving multi-op editor (engine.patch_editor).
        Preserves VBA / pivots / sparklines / conditional formatting. Supports
        ops: set_cell, fix_formula, replace_text, insert_row, add_column,
        rename_sheet, delete_row. Patches apply in order; later ops see earlier
        mutations. This is the PREFERRED edit engine for most edits.

    --set-cells '[{addr:"B2",value:100}, {addr:"C2",formula:"=SUM(B2:B10)"}]'
        --sheet "<name>"
        Plain openpyxl set_cells round-trip (engine.editor.set_cells).
        ⚠️ Round-trip drops macros / pivots / advanced conditional formatting.
        Use when --patches' set_cell can't express what you need.

    --add-sheet '{"sheet_name":"X","after":"Y","headers":["a","b"]}'
        Append a new tab (engine.editor.add_sheet).

    --add-chart '{"sheet":"X","chart_type":"bar","data_range":"B1:B10",
                  "categories_range":"A2:A10","title":"Q3","anchor":"H2"}'
        Insert a native chart (engine.chart_builder.add_chart).
        ⚠️ Also openpyxl round-trip (same caveat as set-cells).

Each engine accepts a ``-file`` variant for large payloads:
    --patches-file /workspace/patches.json   etc.

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage examples:
    apply_edits.py --input in.xlsx --output out.xlsx \\
      --patches '[{"op":"set_cell","sheet":"Q3","cell":"B3","value":1200}]'

    apply_edits.py --input in.xlsx --output out.xlsx \\
      --set-cells '[{"addr":"B2","value":100}]' --sheet "Data"

    apply_edits.py --input in.xlsx --output out.xlsx \\
      --add-chart '{"sheet":"Data","chart_type":"bar","data_range":"B1:B10","anchor":"H2"}'
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    load_json_arg_or_file,
    parse_json_arg,
    staged_workdir,
)


def _ensure_xlsx(name: str) -> str:
    return name if name.endswith(".xlsx") else name + ".xlsx"


def _pick_engine(args: argparse.Namespace) -> str:
    """Return exactly one of: 'patches' / 'set_cells' / 'add_sheet' / 'add_chart'.

    Errors out if zero or multiple engines were specified.
    """
    chosen = []
    if args.patches is not None or args.patches_file is not None:
        chosen.append("patches")
    if args.set_cells is not None or args.set_cells_file is not None:
        chosen.append("set_cells")
    if args.add_sheet is not None or args.add_sheet_file is not None:
        chosen.append("add_sheet")
    if args.add_chart is not None or args.add_chart_file is not None:
        chosen.append("add_chart")
    if not chosen:
        emit_error(
            "ValueError",
            "must provide exactly one of: --patches / --set-cells / --add-sheet / --add-chart "
            "(or their -file variants)",
            exit_code=2,
        )
    if len(chosen) > 1:
        emit_error(
            "ValueError",
            f"multiple edit engines specified ({chosen}); pick exactly one per call",
            exit_code=2,
        )
    return chosen[0]


def cmd_patches(input_path: str, output_path: str, patches: list[dict]) -> None:
    from engine.patch_editor import apply_patches  # type: ignore

    if not isinstance(patches, list) or not patches:
        emit_error("ValueError", "patches must be a non-empty list", exit_code=2)

    with staged_workdir(
        {"in.xlsx": input_path}, output_name="out.xlsx", output_dst=output_path
    ):
        result = apply_patches(
            input_filename="in.xlsx",
            output_filename="out.xlsx",
            patches=patches,
        )
    emit_json({"ok": True, "meta": result})


def cmd_set_cells(
    input_path: str, output_path: str, sheet: str, cells: list[dict]
) -> None:
    from engine.editor import set_cells  # type: ignore

    if not isinstance(cells, list) or not cells:
        emit_error("ValueError", "--set-cells must decode to a non-empty list", exit_code=2)

    with staged_workdir(
        {"in.xlsx": input_path}, output_name="out.xlsx", output_dst=output_path
    ):
        result = set_cells(
            input_filename="in.xlsx",
            output_filename="out.xlsx",
            sheet=sheet,
            cells=cells,
        )
    emit_json({"ok": True, "meta": result})


def cmd_add_sheet(input_path: str, output_path: str, payload: dict) -> None:
    from engine.editor import add_sheet  # type: ignore

    if not isinstance(payload, dict):
        emit_error("ValueError", "--add-sheet must decode to an object", exit_code=2)
    sheet_name = payload.get("sheet_name") or payload.get("name")
    if not sheet_name or not str(sheet_name).strip():
        emit_error("ValueError", "add-sheet payload requires 'sheet_name'", exit_code=2)

    with staged_workdir(
        {"in.xlsx": input_path}, output_name="out.xlsx", output_dst=output_path
    ):
        result = add_sheet(
            input_filename="in.xlsx",
            output_filename="out.xlsx",
            sheet_name=str(sheet_name).strip(),
            after=payload.get("after"),
            headers=payload.get("headers"),
        )
    emit_json({"ok": True, "meta": result})


def cmd_add_chart(input_path: str, output_path: str, payload: dict) -> None:
    from engine.chart_builder import add_chart  # type: ignore

    if not isinstance(payload, dict):
        emit_error("ValueError", "--add-chart must decode to an object", exit_code=2)
    for required in ("sheet", "chart_type", "data_range"):
        if not payload.get(required):
            emit_error(
                "ValueError",
                f"add-chart payload requires '{required}'",
                exit_code=2,
            )
    if payload["chart_type"] not in ("bar", "line", "pie"):
        emit_error(
            "ValueError",
            f"chart_type must be one of bar/line/pie (got {payload['chart_type']!r})",
            exit_code=2,
        )

    with staged_workdir(
        {"in.xlsx": input_path}, output_name="out.xlsx", output_dst=output_path
    ):
        result = add_chart(
            input_filename="in.xlsx",
            output_filename="out.xlsx",
            sheet=payload["sheet"],
            chart_type=payload["chart_type"],
            data_range=payload["data_range"],
            categories_range=payload.get("categories_range"),
            title=payload.get("title"),
            x_title=payload.get("x_title"),
            y_title=payload.get("y_title"),
            anchor=payload.get("anchor", "H2"),
        )
    emit_json({"ok": True, "meta": result})


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--input", required=True, help="path to source .xlsx")
    p.add_argument("--output", required=True, help="path to write edited .xlsx")

    # Engine selectors — mutually exclusive; checked in _pick_engine
    p.add_argument("--patches", help="JSON array of patch ops (byte-preserving)")
    p.add_argument("--patches-file", help="path to JSON file containing the patches array")

    p.add_argument("--set-cells", dest="set_cells", help="JSON array of cell specs")
    p.add_argument(
        "--set-cells-file", dest="set_cells_file", help="path to JSON file containing the cells array"
    )
    p.add_argument(
        "--sheet",
        help="[--set-cells] target sheet name (required when using --set-cells)",
    )

    p.add_argument(
        "--add-sheet", dest="add_sheet",
        help='JSON object for new sheet (e.g. \'{"sheet_name":"X","after":"Y","headers":[...]}\')',
    )
    p.add_argument(
        "--add-sheet-file", dest="add_sheet_file",
        help="path to JSON file containing the add-sheet payload",
    )

    p.add_argument(
        "--add-chart", dest="add_chart",
        help='JSON object for chart insertion (sheet, chart_type, data_range, ...)',
    )
    p.add_argument(
        "--add-chart-file", dest="add_chart_file",
        help="path to JSON file containing the add-chart payload",
    )

    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    output_path = _ensure_xlsx(args.output)
    engine = _pick_engine(args)

    try:
        if engine == "patches":
            patches = load_json_arg_or_file(args.patches, args.patches_file, "patches")
            cmd_patches(args.input, output_path, patches)
        elif engine == "set_cells":
            if not args.sheet:
                emit_error(
                    "ValueError",
                    "--sheet is required when using --set-cells",
                    exit_code=2,
                )
            cells = load_json_arg_or_file(args.set_cells, args.set_cells_file, "set-cells")
            cmd_set_cells(args.input, output_path, args.sheet, cells)
        elif engine == "add_sheet":
            payload = load_json_arg_or_file(args.add_sheet, args.add_sheet_file, "add-sheet")
            cmd_add_sheet(args.input, output_path, payload)
        elif engine == "add_chart":
            payload = load_json_arg_or_file(args.add_chart, args.add_chart_file, "add-chart")
            cmd_add_chart(args.input, output_path, payload)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
