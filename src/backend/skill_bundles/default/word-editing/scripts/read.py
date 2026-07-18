#!/usr/bin/env python3
"""read.py — read-only inspection of a .docx file.

Four modes (mutually exclusive):

    --mode text         全文文本（可选段落范围切片）
    --mode outline      标题树（只回 heading + level + paragraph_index）
    --mode placeholders 列出文档里所有 {{xxx}} 占位符（regex 可配置）
    --mode analyze      完整盘点（节数 / 表 / 图 / 自定义样式 / 字数；走 .NET CLI）

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage examples:
    read.py --mode outline --input /workspace/doc.docx
    read.py --mode text    --input /workspace/doc.docx --paragraph-range 0,50
    read.py --mode placeholders --input /workspace/doc.docx --pattern '{{(\\w+)}}'
    read.py --mode analyze --input /workspace/doc.docx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    parse_json_arg,
    run_dotnet,
    staged_workdir,
)


def cmd_text(input_path: str, paragraph_range: str | None) -> None:
    from engine.reader import get_text  # type: ignore

    pr_tuple = None
    if paragraph_range:
        parsed = parse_json_arg(f"[{paragraph_range}]", "paragraph-range")
        if (
            not isinstance(parsed, list)
            or len(parsed) != 2
            or not all(isinstance(x, int) for x in parsed)
        ):
            emit_error(
                "ValueError",
                "--paragraph-range must be 'start,end' (two ints)",
                exit_code=2,
            )
        pr_tuple = tuple(parsed)

    with staged_workdir({"input.docx": input_path}):
        result = get_text(input_filename="input.docx", paragraph_range=pr_tuple)
    emit_json({"ok": True, "meta": result})


def cmd_outline(input_path: str) -> None:
    from engine.reader import get_outline  # type: ignore

    with staged_workdir({"input.docx": input_path}):
        result = get_outline(input_filename="input.docx")
    emit_json({"ok": True, "meta": result})


def cmd_placeholders(input_path: str, pattern: str) -> None:
    from engine.editor import list_placeholders  # type: ignore

    with staged_workdir({"input.docx": input_path}):
        result = list_placeholders(input_filename="input.docx", pattern=pattern)
    emit_json({"ok": True, "meta": result})


def cmd_analyze(input_path: str) -> None:
    with staged_workdir({"in.docx": input_path}) as workdir:
        try:
            proc = run_dotnet(
                "analyze",
                ["--input", "in.docx", "--json"],
                cwd=workdir,
            )
        except FileNotFoundError as exc:
            emit_error("RuntimeMissing", str(exc))
        if proc.returncode not in (0,):
            emit_error(
                "DotnetError",
                f"analyze failed (exit {proc.returncode})",
                extra={"stdout": proc.stdout, "stderr": proc.stderr},
            )
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            emit_error(
                "ParseError",
                f"analyze stdout not valid JSON: {exc}",
                extra={"stdout": proc.stdout[:2000]},
            )
            return  # unreachable
        emit_json({"ok": True, "meta": {"report": report}})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True, choices=["text", "outline", "placeholders", "analyze"])
    p.add_argument("--input", required=True, help="path to .docx file")
    p.add_argument("--paragraph-range", help="[text mode] e.g. '0,50' for first 50 paras (0-based, half-open)")
    p.add_argument("--pattern", default=r"\{\{(\w+)\}\}", help="[placeholders mode] regex with one capture group")
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    try:
        if args.mode == "text":
            cmd_text(args.input, args.paragraph_range)
        elif args.mode == "outline":
            cmd_outline(args.input)
        elif args.mode == "placeholders":
            cmd_placeholders(args.input, args.pattern)
        elif args.mode == "analyze":
            cmd_analyze(args.input)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
