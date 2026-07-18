#!/usr/bin/env python3
"""apply_template.py — copy a template's formatting onto a source's content.

Use when the user has:
    - A SOURCE .docx whose CONTENT they want to keep (text, headings, tables…)
    - A TEMPLATE .docx whose STYLE they want to copy (fonts, theme colors,
      list numbering, page setup, optionally page headers/footers)

Typical phrasings: "按这个模板套样式" / "把这份内容用那份模板的格式重排" /
"apply this template to my draft".

Under the hood: copies the template's styles.xml / theme1.xml / numbering.xml
/ section properties (and optionally header/footer parts) into the source.
Run-level direct formatting in the source still takes precedence over styles —
if the output still doesn't look right, the source likely has inline rPr/pPr
overrides that need stripping (consider running apply_edits.py with a format
op afterwards).

Usage:
    apply_template.py --source draft.docx --template ref.docx --output out.docx
    apply_template.py --source draft.docx --template ref.docx --output out.docx \\
        --no-apply-headers-footers --no-apply-theme

Flags default to TRUE for styles/theme/numbering/sections, FALSE for
headers/footers (multi-section templates with per-section headers/footers
often produce surprising results; enable only when you've checked the
template is single-section).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import emit_error, emit_json, run_dotnet, staged_workdir


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--source", required=True, help="content donor .docx")
    p.add_argument("--template", required=True, help="formatting donor .docx")
    p.add_argument("--output", required=True, help="output .docx path")

    p.add_argument("--apply-styles", dest="apply_styles", action="store_true", default=True)
    p.add_argument("--no-apply-styles", dest="apply_styles", action="store_false")
    p.add_argument("--apply-theme", dest="apply_theme", action="store_true", default=True)
    p.add_argument("--no-apply-theme", dest="apply_theme", action="store_false")
    p.add_argument("--apply-numbering", dest="apply_numbering", action="store_true", default=True)
    p.add_argument("--no-apply-numbering", dest="apply_numbering", action="store_false")
    p.add_argument("--apply-sections", dest="apply_sections", action="store_true", default=True)
    p.add_argument("--no-apply-sections", dest="apply_sections", action="store_false")
    p.add_argument(
        "--apply-headers-footers",
        dest="apply_headers_footers",
        action="store_true",
        default=False,
        help="copy page header/footer parts; off by default because multi-section "
             "templates produce surprising results — enable only when checked",
    )
    args = p.parse_args()

    for path_arg, label in [(args.source, "source"), (args.template, "template")]:
        if not Path(path_arg).is_file():
            emit_error("FileNotFound", f"{label} not found: {path_arg}", exit_code=2)

    final_name = Path(args.output).name
    cli_args = [
        "--input", "source.docx",
        "--template", "template.docx",
        "--output", final_name,
    ]
    if not args.apply_styles:
        cli_args += ["--apply-styles", "false"]
    if not args.apply_theme:
        cli_args += ["--apply-theme", "false"]
    if not args.apply_numbering:
        cli_args += ["--apply-numbering", "false"]
    if not args.apply_sections:
        cli_args += ["--apply-sections", "false"]
    if args.apply_headers_footers:
        cli_args += ["--apply-headers-footers", "true"]

    applied: list[str] = []
    if args.apply_styles: applied.append("styles")
    if args.apply_theme: applied.append("theme")
    if args.apply_numbering: applied.append("numbering")
    if args.apply_sections: applied.append("sections")
    if args.apply_headers_footers: applied.append("headers_footers")

    try:
        with staged_workdir(
            {"source.docx": args.source, "template.docx": args.template},
            output_name=final_name,
            output_dst=args.output,
        ) as workdir:
            proc = run_dotnet("apply-template", cli_args, cwd=workdir, timeout=120)
    except FileNotFoundError as exc:
        emit_error("RuntimeMissing", str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))
        return

    if proc.returncode != 0:
        emit_error(
            "DotnetError",
            f"apply-template failed (exit {proc.returncode})",
            extra={"stdout": proc.stdout, "stderr": proc.stderr},
        )

    emit_json({
        "ok": True,
        "meta": {
            "output": args.output,
            "applied": applied,
            "stdout_tail": proc.stdout[-500:] if proc.stdout else "",
        },
    })


if __name__ == "__main__":
    main()
