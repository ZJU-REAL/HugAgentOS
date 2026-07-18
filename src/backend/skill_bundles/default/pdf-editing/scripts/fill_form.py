#!/usr/bin/env python3
"""fill_form.py — write values into a PDF's AcroForm fields.

Wraps ``engine.form.fill_fields``. Always run ``pdf-cli read --mode
form-fields`` first to discover the field names and constraints (allowed
dropdown values, radio options, etc.).

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage:
    fill_form.py --input /workspace/form.pdf --output /workspace/filled.pdf \\
      --fields '{"Name":"张三","BirthDate":"1990-01-01","Newsletter":"yes"}'

    fill_form.py --input form.pdf --output filled.pdf --fields-file /workspace/fields.json
"""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    load_json_arg_or_file,
    staged_workdir,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="path to source .pdf")
    p.add_argument("--output", required=True, help="path to write the filled .pdf")
    p.add_argument(
        "--fields",
        help="JSON object mapping field_name → value (string only)",
    )
    p.add_argument(
        "--fields-file",
        help="path to JSON file containing the fields object",
    )
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    field_values = load_json_arg_or_file(args.fields, args.fields_file, "fields")
    if not isinstance(field_values, dict) or not field_values:
        emit_error("ValueError", "--fields must decode to a non-empty object", exit_code=2)

    out_path = args.output if args.output.lower().endswith(".pdf") else args.output + ".pdf"

    from engine.form import fill_fields  # type: ignore

    try:
        with staged_workdir(
            {"input.pdf": args.input},
            output_name="output.pdf",
            output_dst=out_path,
        ):
            result = fill_fields(
                input_filename="input.pdf",
                output_filename="output.pdf",
                field_values=field_values,
            )
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))

    emit_json({"ok": True, "meta": result})


if __name__ == "__main__":
    main()
