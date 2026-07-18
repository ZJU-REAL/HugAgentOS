#!/usr/bin/env python3
"""word-cli — single dispatcher for the word-editing skill.

Thin shim: parses ``word-cli <subcommand> ...`` and ``exec``s the
corresponding ``<subcommand>.py`` script in this directory with the
remaining argv. Because we use ``os.execv``, the subscript replaces the
current process — no subprocess wrapper, no double output buffering,
exit code propagates naturally.

Subcommands:
    read        inspect a .docx (text / outline / placeholders / analyze)
    create      generate a new .docx (markdown or structured)
    edit        batch-apply edit ops to an existing .docx (15 ops)
    template    copy a template's formatting onto a source's content
    validate    XSD + business-rule check, optionally with auto-repair
    diff        compare two .docx files
    convert     .doc→.docx or .docx→.pdf (LibreOffice)

Per-subcommand help: ``word-cli <subcommand> --help``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# subcommand → script filename in this directory
_SUBCOMMANDS: dict[str, str] = {
    "read":     "read.py",
    "create":   "create.py",
    "edit":     "apply_edits.py",
    "template": "apply_template.py",
    "validate": "validate.py",
    "diff":     "diff.py",
    "convert":  "convert.py",
}


def _print_help() -> None:
    print(__doc__.strip())
    print()
    print("Available subcommands:")
    for cmd in _SUBCOMMANDS:
        print(f"  {cmd}")
    print()
    print("Examples:")
    print("  word-cli read --mode outline --input doc.docx")
    print("  word-cli edit --input in.docx --output out.docx --ops '[...]'")
    print("  word-cli validate --input doc.docx --repair --output fixed.docx")


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help()
        sys.exit(0 if argv else 2)

    cmd = argv[0]
    if cmd not in _SUBCOMMANDS:
        print(f"word-cli: unknown subcommand: {cmd!r}", file=sys.stderr)
        print(f"Available: {', '.join(_SUBCOMMANDS)}", file=sys.stderr)
        sys.exit(2)

    script = Path(__file__).resolve().parent / _SUBCOMMANDS[cmd]
    if not script.is_file():
        print(f"word-cli: implementation script not found: {script}", file=sys.stderr)
        sys.exit(2)

    # Replace current process. argv[0] becomes the python interpreter; the
    # script gets argv[1:] = [script_path, *remaining_user_args].
    os.execv(sys.executable, [sys.executable, str(script), *argv[1:]])


if __name__ == "__main__":
    main()
