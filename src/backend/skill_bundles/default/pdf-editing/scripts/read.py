#!/usr/bin/env python3
"""read.py — read-only inspection of a .pdf file.

Five modes (mutually exclusive):

    --mode text         全文文本提取（可指定 1-based 页号列表）
    --mode outline      书签 / 目录（含 page 与 level）
    --mode metadata     文档信息：页数、标题、作者、是否加密
    --mode overview     metadata + outline 合并（"第一眼"模式）
    --mode form-fields  列出 AcroForm 表单字段（含类型、默认值、可选值）

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage examples:
    read.py --mode text     --input /workspace/doc.pdf
    read.py --mode text     --input /workspace/doc.pdf --pages 1,3,5
    read.py --mode outline  --input /workspace/doc.pdf
    read.py --mode metadata --input /workspace/doc.pdf
    read.py --mode overview --input /workspace/doc.pdf
    read.py --mode form-fields --input /workspace/form.pdf
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    staged_workdir,
)


def _parse_pages(raw: str | None) -> list[int] | None:
    if raw is None or not raw.strip():
        return None
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError as exc:
        emit_error(
            "ValueError",
            f"--pages must be comma-separated 1-based integers: {exc}",
            exit_code=2,
        )
        return None  # unreachable


def cmd_text(input_path: str, pages: list[int] | None) -> None:
    from engine.reader import get_text  # type: ignore

    with staged_workdir({"input.pdf": input_path}):
        result = get_text(input_filename="input.pdf", pages=pages)
    emit_json({"ok": True, "meta": result})


def cmd_outline(input_path: str) -> None:
    from engine.reader import get_outline  # type: ignore

    with staged_workdir({"input.pdf": input_path}):
        result = get_outline(input_filename="input.pdf")
    emit_json({"ok": True, "meta": result})


def cmd_metadata(input_path: str) -> None:
    from engine.reader import get_metadata  # type: ignore

    with staged_workdir({"input.pdf": input_path}):
        result = get_metadata(input_filename="input.pdf")
    emit_json({"ok": True, "meta": result})


def cmd_overview(input_path: str) -> None:
    """metadata + outline merged (replaces the old pdf_open_document MCP tool)."""
    from engine.reader import get_metadata, get_outline  # type: ignore

    with staged_workdir({"input.pdf": input_path}):
        md = get_metadata(input_filename="input.pdf")
        ol = get_outline(input_filename="input.pdf")
    emit_json({"ok": True, "meta": {**md, **ol}})


def cmd_form_fields(input_path: str) -> None:
    from engine.form import inspect_fields  # type: ignore

    with staged_workdir({"input.pdf": input_path}):
        result = inspect_fields(input_filename="input.pdf")
    emit_json({"ok": True, "meta": result})


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--mode",
        required=True,
        choices=["text", "outline", "metadata", "overview", "form-fields"],
    )
    p.add_argument("--input", required=True, help="path to .pdf file")
    p.add_argument(
        "--pages",
        help="[text mode] comma-separated 1-based page numbers (e.g. '1,3,5'); omit = all",
    )
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    try:
        if args.mode == "text":
            cmd_text(args.input, _parse_pages(args.pages))
        elif args.mode == "outline":
            cmd_outline(args.input)
        elif args.mode == "metadata":
            cmd_metadata(args.input)
        elif args.mode == "overview":
            cmd_overview(args.input)
        elif args.mode == "form-fields":
            cmd_form_fields(args.input)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
