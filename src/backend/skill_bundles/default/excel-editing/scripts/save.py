#!/usr/bin/env python3
"""save.py — copy a .xlsx to a finalized display name.

Effectively just ``cp <input> <output>`` with .xlsx suffix enforcement and
JSON reporting, so the LLM can treat "save as" as a first-class subcommand
without having to mix in raw shell commands.

The original MCP ``excel_save_workbook`` re-registered the artifact under a
new name; in the skill model the agent registers the produced file via
``sandbox_get_artifact`` after this script writes the destination path —
the rename is purely cosmetic for the artifact name.

Output: a single JSON line to stdout.

Usage:
    save.py --input /workspace/edit.xlsx --output /workspace/最终版.xlsx
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from _common import emit_error, emit_json


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="path to source .xlsx")
    p.add_argument("--output", required=True, help="path to write under the final name (must end .xlsx)")
    args = p.parse_args()

    src = Path(args.input)
    if not src.is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    dst = Path(args.output)
    if dst.suffix.lower() != ".xlsx":
        emit_error(
            "ValueError",
            f"--output must end with .xlsx (got {dst.suffix or 'no suffix'})",
            exit_code=2,
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))

    emit_json(
        {
            "ok": True,
            "meta": {
                "source": str(src),
                "output": str(dst),
                "size_bytes": dst.stat().st_size,
            },
        }
    )


if __name__ == "__main__":
    main()
