#!/usr/bin/env python3
"""split.py — split a PDF into multiple files by page ranges.

Wraps ``engine.splitter.split``. Each range becomes one output file
in the chosen output directory (or under the names you specify).

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage:
    split.py --input /workspace/doc.pdf --output-dir /workspace/parts --ranges 1-3,4-6,7
    split.py --input /workspace/doc.pdf --output-dir /workspace/parts \\
        --ranges 1-3,4-6 --names chapter1.pdf chapter2.pdf
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from _common import emit_error, emit_json, staged_workdir


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="path to source .pdf")
    p.add_argument(
        "--output-dir",
        required=True,
        help="directory to write the per-range PDFs into (created if missing)",
    )
    p.add_argument(
        "--ranges",
        required=True,
        help="comma-separated 1-based page ranges, e.g. '1-3,4-6,7'",
    )
    p.add_argument(
        "--names",
        nargs="+",
        help="optional list of output filenames (length must match --ranges); "
             "default: part_1.pdf, part_2.pdf, ...",
    )
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    page_ranges = [r.strip() for r in args.ranges.split(",") if r.strip()]
    if not page_ranges:
        emit_error("ValueError", "--ranges must contain at least one range", exit_code=2)
    if args.names is not None and len(args.names) != len(page_ranges):
        emit_error(
            "ValueError",
            f"--names length {len(args.names)} != --ranges length {len(page_ranges)}",
            exit_code=2,
        )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from engine.splitter import split  # type: ignore

    try:
        with staged_workdir({"input.pdf": args.input}) as workdir:
            result = split(
                input_filename="input.pdf",
                page_ranges=page_ranges,
                output_filenames=args.names,
            )
            # Copy each produced file to the user-specified output_dir.
            for out_info in result["outputs"]:
                produced = workdir / out_info["filename"]
                if not produced.is_file():
                    emit_error(
                        "OutputMissing",
                        f"splitter did not produce {out_info['filename']!r}",
                    )
                dst = out_dir / out_info["filename"]
                shutil.copy2(produced, dst)
                out_info["path"] = str(dst)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))

    emit_json({"ok": True, "meta": result})


if __name__ == "__main__":
    main()
