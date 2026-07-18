#!/usr/bin/env python3
"""excel-cli — single dispatcher for the excel-editing skill.

Thin shim: parses ``excel-cli <subcommand> ...`` and ``exec``s the
corresponding ``<subcommand>.py`` script in this directory with the
remaining argv. Because we use ``os.execv``, the subscript replaces the
current process — no subprocess wrapper, no double output buffering,
exit code propagates naturally.

Subcommands:
    read        inspect a .xlsx (summary / sheet / validate-formulas)
    create      generate a new .xlsx (plain workbook or formula-first model)
    edit        modify an existing .xlsx — patches / set-cells / add-sheet / add-chart
    save        copy a .xlsx to a finalized display name
    convert     .xlsx → .pdf (LibreOffice headless)

Per-subcommand help: ``excel-cli <subcommand> --help``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# subcommand → script filename in this directory
_SUBCOMMANDS: dict[str, str] = {
    "read":    "read.py",
    "create":  "create.py",
    "edit":    "apply_edits.py",
    "save":    "save.py",
    "convert": "convert.py",
}


def _print_help() -> None:
    print(__doc__.strip())
    print()
    print("Available subcommands:")
    for cmd in _SUBCOMMANDS:
        print(f"  {cmd}")
    print()
    print("Examples:")
    print("  excel-cli read --mode summary --input wb.xlsx")
    print("  excel-cli create --mode workbook --output out.xlsx --sheets '[...]'")
    print("  excel-cli edit --input in.xlsx --output out.xlsx --patches '[...]'")
    print("  excel-cli convert --to pdf --input in.xlsx --output out.pdf")


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help()
        sys.exit(0 if argv else 2)

    cmd = argv[0]
    if cmd not in _SUBCOMMANDS:
        print(f"excel-cli: unknown subcommand: {cmd!r}", file=sys.stderr)
        print(f"Available: {', '.join(_SUBCOMMANDS)}", file=sys.stderr)
        sys.exit(2)

    script = Path(__file__).resolve().parent / _SUBCOMMANDS[cmd]
    if not script.is_file():
        print(f"excel-cli: implementation script not found: {script}", file=sys.stderr)
        sys.exit(2)

    os.execv(sys.executable, [sys.executable, str(script), *argv[1:]])


if __name__ == "__main__":
    main()
