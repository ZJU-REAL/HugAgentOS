#!/usr/bin/env python3
"""create.py — generate a print-quality, designed PDF from a spec.

Wraps ``engine.creator.create``. Spec is a JSON object describing
title, doc_type (cover style), and a content[] block list. Supports image
blocks ({"type":"image","path":"<file>"}) — pass each external image via
``--image local_name=/abs/path/to/img.png`` and reference ``local_name`` in
the spec's ``path``.

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage:
    create.py --output /workspace/report.pdf --spec-file /workspace/spec.json

    create.py --output /workspace/report.pdf --spec-file /workspace/spec.json \\
      --image chart1=/workspace/chart1.png --image cover=/workspace/cover.jpg

spec example (write to /workspace/spec.json):
    {
      "title": "Q3 产业链分析",
      "doc_type": "report",
      "author": "工信局",
      "date": "2026-05",
      "content": [
        {"type": "h1", "text": "概述"},
        {"type": "body", "text": "本季度..."},
        {"type": "chart", "chart_type": "bar",
         "labels": ["华东","华南"], "datasets": [{"values":[125,98]}]},
        {"type": "image", "path": "chart1", "caption": "图1"}
      ]
    }
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    load_json_arg_or_file,
    staged_workdir,
)


def _parse_image_mappings(raw: list[str]) -> dict[str, str]:
    """Parse repeated --image local_id=/abs/path/to/img.png args."""
    out: dict[str, str] = {}
    for entry in raw or []:
        if "=" not in entry:
            emit_error(
                "ValueError",
                f"--image must be 'local_id=/abs/path', got {entry!r}",
                exit_code=2,
            )
        local_id, abs_path = entry.split("=", 1)
        local_id, abs_path = local_id.strip(), abs_path.strip()
        if not local_id or not abs_path:
            emit_error(
                "ValueError",
                f"--image entry has empty local_id or path: {entry!r}",
                exit_code=2,
            )
        out[local_id] = abs_path
    return out


def _rewrite_image_paths(spec: dict, image_map: dict[str, str]) -> dict[str, str]:
    """Walk the spec content list; for each image/figure block whose path
    is a known local_id, swap it to a basename to be staged into workdir.

    Returns a {basename: source_abs_path} mapping for staging.
    """
    to_stage: dict[str, str] = {}
    used: set[str] = set()
    content = spec.get("content")
    if isinstance(content, list):
        for i, blk in enumerate(content):
            if not isinstance(blk, dict):
                continue
            if blk.get("type") in ("image", "figure"):
                raw = blk.get("path") or blk.get("src")
                if raw and raw in image_map:
                    src = image_map[raw]
                    basename = f"_img_{i:02d}{Path(src).suffix or '.png'}"
                    blk["path"] = basename
                    to_stage[basename] = src
                    used.add(raw)
    # cover image
    cover_raw = spec.get("cover_image")
    if cover_raw and cover_raw in image_map:
        src = image_map[cover_raw]
        basename = f"_cover_img{Path(src).suffix or '.jpg'}"
        spec["cover_image"] = basename
        to_stage[basename] = src
        used.add(cover_raw)

    unused = set(image_map.keys()) - used
    if unused:
        emit_error(
            "ValueError",
            f"--image entries not referenced in spec: {sorted(unused)}",
            exit_code=2,
        )
    return to_stage


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", required=True, help="path to write the .pdf into")
    p.add_argument("--spec", help="JSON spec object")
    p.add_argument("--spec-file", help="path to JSON file containing the spec")
    p.add_argument(
        "--image",
        action="append",
        help="image mapping local_id=/abs/path.png (repeatable; referenced from spec)",
    )
    args = p.parse_args()

    spec = load_json_arg_or_file(args.spec, args.spec_file, "spec")
    if not isinstance(spec, dict):
        emit_error("ValueError", "--spec must decode to a JSON object", exit_code=2)

    image_map = _parse_image_mappings(args.image or [])
    to_stage = _rewrite_image_paths(spec, image_map)

    out_path = args.output if args.output.lower().endswith(".pdf") else args.output + ".pdf"

    from engine.creator import create  # type: ignore

    try:
        with staged_workdir(
            to_stage or {},
            output_name="output.pdf",
            output_dst=out_path,
        ):
            result = create(spec=spec, output_filename="output.pdf")
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))

    emit_json({"ok": True, "meta": result})


if __name__ == "__main__":
    main()
