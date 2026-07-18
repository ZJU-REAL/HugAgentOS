#!/usr/bin/env python3
"""create.py — generate a new .docx from scratch.

Three input modes (mutually exclusive):

    --markdown <md-text>    扁平 markdown 字面文本，走 python-docx（快，公文字体）
    --markdown-file <path>  指向 .md/.markdown/.txt 文件，脚本读其内容当 markdown（推荐）
    --content  <json>       结构化 sections，走 .NET CLI（封面/目录/页眉页脚/多节）

Usage:
    create.py --markdown-file /workspace/draft.md --output /workspace/draft.docx
    create.py --markdown "$(cat draft.md)" --output /workspace/draft.docx
    create.py --content '{"sections":[...]}' --output /workspace/report.docx \\
              --title "...年度报告" --toc --page-numbers --margins narrow

Output: a single JSON line to stdout. exit 0 on success.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from _common import (
    ASSETS_DIR,
    DOTNET_DLL,
    emit_error,
    emit_json,
    parse_json_arg,
    run_dotnet,
    staged_workdir,
)


def _looks_like_path(value: str) -> bool:
    """True if `value` looks like the LLM passed a file path as --markdown content.

    Heuristic: single line, no markdown markup, ends in a known text extension,
    and (if pointing inside the sandbox workspace) actually exists on disk.
    Catches the common footgun ``--markdown /workspace/draft.md`` where the
    script would otherwise render the literal path string into the docx.
    """
    s = value.strip()
    if not s or "\n" in s:
        return False
    if len(s) > 512:
        return False
    if any(ch in s for ch in "#*`>|"):
        return False
    lower = s.lower()
    if not lower.endswith((".md", ".markdown", ".txt")):
        return False
    try:
        return Path(s).is_file()
    except OSError:
        return False


def cmd_markdown(
    *,
    markdown: str,
    title: str,
    language: str,
    output: str,
) -> None:
    if not markdown.strip():
        emit_error("ValueError", "--markdown must be non-empty", exit_code=2)

    if _looks_like_path(markdown):
        emit_error(
            "MarkdownLooksLikePath",
            f"--markdown expects literal markdown text, but got what looks like "
            f"a file path ({markdown!r}). Use --markdown-file <path> instead, "
            "or wrap with --markdown \"$(cat path.md)\".",
            exit_code=2,
        )

    # engine.markdown_engine is the canonical markdown→docx bytes
    # path; the skill stays independent of any sibling MCP server's internals.
    from engine.markdown_engine import markdown_to_docx_bytes as _markdown_to_docx_bytes  # type: ignore

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        blob = _markdown_to_docx_bytes(markdown=markdown, title=title)
    except Exception as exc:  # noqa: BLE001
        emit_error("MarkdownEngineError", str(exc))
        return

    out_path.write_bytes(blob)
    emit_json({"ok": True, "meta": {
        "engine": "markdown",
        "output": str(out_path),
        "size_bytes": len(blob),
        "title": title,
        "language": language,
    }})


def _build_static_toc_section(
    content_dict: dict, *, language: str = "zh"
) -> dict | None:
    """Pre-populate a static "目录" / "Table of Contents" section.

    Self-contained TOC builder (formerly mirrored from the now-deleted
    word_mcp wrapper) so the skill has no cross-package dependency.
    """
    title = "目录" if language == "zh" else "Table of Contents"
    headings: list[tuple[int, str]] = []

    def _walk(sections):
        if not isinstance(sections, list):
            return
        for s in sections:
            if not isinstance(s, dict):
                continue
            heading = s.get("heading")
            if isinstance(heading, str) and heading.strip():
                level = s.get("level", 1)
                if not isinstance(level, int) or level < 1:
                    level = 1
                headings.append((level, heading.strip()))
            nested = s.get("sections")
            if nested:
                _walk(nested)

    _walk(content_dict.get("sections", []))
    if not headings:
        return None
    paragraphs = [("    " * max(0, lvl - 1)) + text for lvl, text in headings]
    return {"heading": title, "level": 1, "paragraphs": paragraphs}


def cmd_content(
    *,
    content_raw: str,
    title: str | None,
    author: str | None,
    type_: str,
    page_size: str,
    margins: str,
    header: str | None,
    footer: str | None,
    page_numbers: bool,
    toc: bool,
    language: str,
    output: str,
) -> None:
    content_dict = parse_json_arg(content_raw, "content")
    if not isinstance(content_dict, dict):
        emit_error(
            "ValueError",
            "--content must decode to a JSON object with 'sections'",
            exit_code=2,
        )
    content_dict["sections"] = list(content_dict.get("sections", []))

    toc_entries = 0
    if toc:
        toc_section = _build_static_toc_section(content_dict, language=language)
        if toc_section is not None:
            content_dict["sections"].insert(0, toc_section)
            toc_entries = len(toc_section["paragraphs"])

    content_json_inline = json.dumps(content_dict, ensure_ascii=False)
    final_name = Path(output).name

    args: list[str] = [
        "--output", final_name,
        "--type", type_,
        "--page-size", page_size,
        "--margins", margins,
        "--content-json", content_json_inline,
    ]
    if title is not None:
        args += ["--title", title]
    if author is not None:
        args += ["--author", author]
    if header is not None:
        args += ["--header", header]
    if footer is not None:
        args += ["--footer", footer]
    if page_numbers:
        args += ["--page-numbers"]

    with staged_workdir({}, output_name=final_name, output_dst=str(output)) as workdir:
        try:
            proc = run_dotnet("create", args, cwd=workdir)
        except FileNotFoundError as exc:
            emit_error("RuntimeMissing", str(exc))
        if proc.returncode != 0:
            emit_error(
                "DotnetError",
                f"create failed (exit {proc.returncode})",
                extra={"stdout": proc.stdout, "stderr": proc.stderr},
            )

    # Post-process the structural output so section mode renders with the same
    # 方正 fonts / 公文 paragraph formatting / black headings / centered tables
    # as markdown mode. Best-effort: a valid .docx already exists either way.
    harmonized = False
    harmonize_error = ""
    try:
        from engine.markdown_engine import harmonize_to_chinese_style  # type: ignore

        out_path = Path(output)
        out_path.write_bytes(harmonize_to_chinese_style(out_path.read_bytes()))
        harmonized = True
    except Exception as exc:  # noqa: BLE001
        harmonize_error = str(exc)

    meta = {
        "engine": "structural",
        "type": type_,
        "page_size": page_size,
        "margins": margins,
        "output": str(output),
        "harmonized": harmonized,
        "stdout_tail": proc.stdout[-500:] if proc.stdout else "",
    }
    if harmonize_error:
        meta["harmonize_error"] = harmonize_error
    if toc and toc_entries:
        meta["toc_injected"] = toc_entries
    emit_json({"ok": True, "meta": meta})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--markdown",
        help="Inline markdown source string. For files prefer --markdown-file (or use $(cat file.md)).",
    )
    group.add_argument(
        "--markdown-file",
        dest="markdown_file",
        help="Path to a .md/.markdown/.txt file; its contents are used as the markdown source.",
    )
    group.add_argument("--content", help="Structural JSON {\"sections\":[...]}")
    p.add_argument("--output", required=True, help="output .docx path")
    p.add_argument("--title", default="报告", help="document title (default: 报告)")
    p.add_argument("--author", help="[content mode only] author / subtitle")
    p.add_argument("--type", dest="type_", default="report",
                   choices=["report", "letter", "memo", "academic"],
                   help="[content mode only] typography preset")
    p.add_argument("--page-size", default="a4", choices=["a4", "letter", "legal", "a3"],
                   help="[content mode only]")
    p.add_argument("--margins", default="standard", choices=["standard", "narrow", "wide"],
                   help="[content mode only]")
    p.add_argument("--header", help="[content mode only] running header text")
    p.add_argument("--footer", help="[content mode only] running footer text")
    p.add_argument("--page-numbers", action="store_true",
                   help="[content mode only] insert page number in footer")
    p.add_argument("--toc", action="store_true",
                   help="[content mode only] prepend a pre-populated static TOC")
    p.add_argument("--language", default="zh", choices=["zh", "en"])
    args = p.parse_args()

    if not args.output.endswith(".docx"):
        args.output += ".docx"

    try:
        if args.markdown is not None or args.markdown_file is not None:
            # markdown mode: reject incompatible flags so the LLM is forced
            # to switch to --content when it needs them.
            bad: list[str] = []
            if args.toc: bad.append("--toc")
            if args.header is not None: bad.append("--header")
            if args.footer is not None: bad.append("--footer")
            if args.page_numbers: bad.append("--page-numbers")
            if args.author is not None: bad.append("--author")
            if args.page_size != "a4": bad.append(f"--page-size={args.page_size}")
            if args.margins != "standard": bad.append(f"--margins={args.margins}")
            if args.type_ != "report": bad.append(f"--type={args.type_}")
            if bad:
                emit_error(
                    "ValueError",
                    f"--markdown mode rejects these flags: {', '.join(bad)}. "
                    "Switch to --content for cover / TOC / page setup.",
                    exit_code=2,
                )

            if args.markdown_file is not None:
                md_path = Path(args.markdown_file)
                if not md_path.is_file():
                    emit_error(
                        "MarkdownFileNotFound",
                        f"--markdown-file does not exist: {args.markdown_file}",
                        exit_code=2,
                    )
                try:
                    markdown_text = md_path.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    emit_error(
                        "MarkdownFileEncoding",
                        f"--markdown-file is not UTF-8 ({md_path}): {exc}",
                        exit_code=2,
                    )
                    return
            else:
                markdown_text = args.markdown

            cmd_markdown(
                markdown=markdown_text,
                title=args.title,
                language=args.language,
                output=args.output,
            )
        else:
            cmd_content(
                content_raw=args.content,
                title=args.title,
                author=args.author,
                type_=args.type_,
                page_size=args.page_size,
                margins=args.margins,
                header=args.header,
                footer=args.footer,
                page_numbers=args.page_numbers,
                toc=args.toc,
                language=args.language,
                output=args.output,
            )
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
