#!/usr/bin/env python3
"""diff.py — compare two .docx files; return text / style / structure differences.

Use when:
    - Verifying an edit didn't accidentally rewrite the whole document
    - Reviewing what changed across a template-application pass
    - Audit / legal review of edits

Granularity is paragraph-level (no sub-paragraph word diff). For fine-grained
text diff, run read.py --mode text on each and use an external diff tool.

Usage:
    diff.py --before original.docx --after edited.docx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import emit_error, emit_json, run_dotnet, staged_workdir


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--before", required=True, help="original .docx")
    p.add_argument("--after", required=True, help="modified .docx")
    args = p.parse_args()

    for path_arg, label in [(args.before, "--before"), (args.after, "--after")]:
        if not Path(path_arg).is_file():
            emit_error("FileNotFound", f"{label} not found: {path_arg}", exit_code=2)

    try:
        with staged_workdir(
            {"before.docx": args.before, "after.docx": args.after}
        ) as workdir:
            proc = run_dotnet(
                "diff",
                ["--before", "before.docx", "--after", "after.docx", "--json"],
                cwd=workdir,
                timeout=90,
            )
    except FileNotFoundError as exc:
        emit_error("RuntimeMissing", str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))
        return

    if proc.returncode != 0:
        emit_error(
            "DotnetError",
            f"diff failed (exit {proc.returncode})",
            extra={"stdout": proc.stdout, "stderr": proc.stderr},
        )

    try:
        diff_report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        emit_error(
            "ParseError",
            f"diff stdout not valid JSON: {exc}",
            extra={"stdout": proc.stdout[:2000]},
        )
        return

    emit_json({"ok": True, "meta": {"diff": diff_report}})


if __name__ == "__main__":
    main()
