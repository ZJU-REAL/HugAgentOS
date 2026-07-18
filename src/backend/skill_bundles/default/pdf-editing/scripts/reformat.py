#!/usr/bin/env python3
"""reformat.py — re-render an existing document into a designed PDF.

Wraps ``engine.creator.reformat``. Accepted source extensions:
.md / .markdown / .txt / .docx / .pdf / .json. Same designed-PDF engine as
``create``, but parses content out of the source instead of taking a JSON
spec.

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage:
    reformat.py --input /workspace/notes.md --output /workspace/notes.pdf
    reformat.py --input draft.docx --output final.pdf --doc-type magazine \\
        --title "年度报告" --author "工信局" --date "2026-05" --accent "#0a5"
"""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import emit_error, emit_json, staged_workdir


_SUPPORTED = {".md", ".markdown", ".txt", ".docx", ".pdf", ".json"}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="source document (md/markdown/txt/docx/pdf/json)")
    p.add_argument("--output", required=True, help="path to write the .pdf into")
    p.add_argument(
        "--doc-type",
        default="report",
        help="cover/design style: report|proposal|resume|portfolio|academic|general|"
             "minimal|stripe|diagonal|frame|editorial|magazine|darkroom|terminal|poster (default report)",
    )
    p.add_argument("--title", default="", help="optional title override")
    p.add_argument("--author", default="", help="optional author for cover/header/footer")
    p.add_argument("--date", default="", help="optional date for cover/header/footer")
    p.add_argument("--accent", default="", help="optional accent colour override (#RRGGBB)")
    args = p.parse_args()

    src = Path(args.input)
    if not src.is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)
    ext = src.suffix.lower()
    if ext not in _SUPPORTED:
        emit_error(
            "ValueError",
            f"unsupported source type {ext!r}; supported: {sorted(_SUPPORTED)}",
            exit_code=2,
        )

    out_path = args.output if args.output.lower().endswith(".pdf") else args.output + ".pdf"

    # Stage with the source extension preserved (creator's reformat() inspects
    # the suffix to pick the parser).
    src_basename = f"input{ext}"

    from engine.creator import reformat  # type: ignore

    try:
        with staged_workdir(
            {src_basename: args.input},
            output_name="output.pdf",
            output_dst=out_path,
        ):
            result = reformat(
                input_filename=src_basename,
                doc_type=args.doc_type or "report",
                output_filename="output.pdf",
                title=args.title,
                author=args.author,
                date=args.date,
                accent=args.accent,
            )
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))

    emit_json({"ok": True, "meta": result})


if __name__ == "__main__":
    main()
