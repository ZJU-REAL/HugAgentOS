#!/usr/bin/env python3
"""pdf-cli — single dispatcher for the pdf-editing skill.

Thin shim: parses ``pdf-cli <subcommand> ...`` and ``exec``s the
corresponding ``<subcommand>.py`` script in this directory with the
remaining argv. Because we use ``os.execv``, the subscript replaces the
current process — no subprocess wrapper, no double output buffering,
exit code propagates naturally.

Subcommands:
    read        inspect a .pdf (text / outline / metadata / form-fields)
    merge       concatenate multiple PDFs in order
    split       split a PDF into per-range output files
    fill-form   write values into AcroForm fields
    create      generate a designed PDF from a spec (cover / charts / formulas)
    reformat    re-render md / docx / txt / pdf into a designed PDF

Per-subcommand help: ``pdf-cli <subcommand> --help``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# subcommand → script filename in this directory
_SUBCOMMANDS: dict[str, str] = {
    "read":      "read.py",
    "merge":     "merge.py",
    "split":     "split.py",
    "fill-form": "fill_form.py",
    "create":    "create.py",
    "reformat":  "reformat.py",
}


def _print_help() -> None:
    print(__doc__.strip())
    print()
    print("Available subcommands:")
    for cmd in _SUBCOMMANDS:
        print(f"  {cmd}")
    print()
    print("Examples:")
    print("  pdf-cli read --mode text --input doc.pdf")
    print("  pdf-cli merge --output out.pdf --inputs a.pdf b.pdf c.pdf")
    print("  pdf-cli split --input doc.pdf --output-dir parts --ranges '1-3,4-6,7'")
    print("  pdf-cli create --output report.pdf --spec-file spec.json")


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help()
        sys.exit(0 if argv else 2)

    cmd = argv[0]
    if cmd not in _SUBCOMMANDS:
        print(f"pdf-cli: unknown subcommand: {cmd!r}", file=sys.stderr)
        print(f"Available: {', '.join(_SUBCOMMANDS)}", file=sys.stderr)
        sys.exit(2)

    script = Path(__file__).resolve().parent / _SUBCOMMANDS[cmd]
    if not script.is_file():
        print(f"pdf-cli: implementation script not found: {script}", file=sys.stderr)
        sys.exit(2)

    os.execv(sys.executable, [sys.executable, str(script), *argv[1:]])


if __name__ == "__main__":
    main()
