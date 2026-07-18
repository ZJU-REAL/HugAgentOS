#!/usr/bin/env python3
"""convert.py — .xlsx → .pdf via LibreOffice headless.

Single mode (only direction supported here): xlsx → pdf. The underlying
LibreOffice invocation needs both ``libreoffice`` (or ``soffice``) and a JRE
on the sandbox image — both are present in the production opensandbox /
script-runner images.

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage:
    convert.py --to pdf --input /workspace/report.xlsx --output /workspace/report.pdf
"""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import emit_error, emit_json, staged_workdir


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--to", required=True, choices=["pdf"], help="conversion target (only 'pdf' supported)")
    p.add_argument("--input", required=True, help="path to source .xlsx")
    p.add_argument("--output", required=True, help="path to write .pdf into")
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    out_path = args.output if args.output.lower().endswith(".pdf") else args.output + ".pdf"

    from engine.libreoffice import to_pdf  # type: ignore

    try:
        with staged_workdir(
            {"input.xlsx": args.input},
            output_name="output.pdf",
            output_dst=out_path,
        ):
            result = to_pdf(
                input_filename="input.xlsx",
                output_filename="output.pdf",
            )
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))

    emit_json({"ok": True, "meta": result})


if __name__ == "__main__":
    main()
