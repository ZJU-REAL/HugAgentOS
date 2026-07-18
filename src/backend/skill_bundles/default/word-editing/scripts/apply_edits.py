#!/usr/bin/env python3
"""apply_edits.py — batch-apply 1..N edit ops to a .docx in a single open/save.

⚡ This is the primary entry for editing existing .docx files. All single-step
editing operations (replace / insert / format / add_table / delete_paragraph /
…) are exposed as ops here instead of as separate scripts. Putting multiple
ops in one --ops array is dramatically cheaper than chaining script invocations
(no extra sandbox_get/put round-trips, and downstream ops see in-memory state
updated by upstream ops — no paragraph-index drift between calls).

15 supported ops (see references/apply-edits-ops.md for full kwargs):

    replace, replace_many, fill_placeholders,
    insert, insert_image, format,
    replace_paragraph, replace_section,
    delete_paragraph, delete_range,
    set_cell_text, fill_table, add_table, move_table,
    update_field

Usage:
    apply_edits.py --input in.docx --output out.docx --ops '[{"op":"replace",...}, ...]'
    apply_edits.py --input in.docx --output out.docx --ops-file ops.json
    apply_edits.py --input in.docx --output out.docx --ops '[...]' --stop-on-error

For insert_image ops, also pass --image <local_id>=<absolute_path> for each
image referenced in the ops (the op's "image_file_id" field is treated as a
local id matched against these mappings). Example:

    apply_edits.py --input in.docx --output out.docx \\
      --image chart1=/workspace/chart1.png \\
      --image logo=/workspace/logo.jpg \\
      --ops '[{"op":"insert_image","image_file_id":"chart1","position":"end","width_inches":4}]'
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    parse_json_arg,
    staged_workdir,
)


def _load_ops(ops_str: str | None, ops_file: str | None) -> list[dict]:
    if (ops_str is None) == (ops_file is None):
        emit_error(
            "ValueError",
            "exactly one of --ops or --ops-file must be provided",
            exit_code=2,
        )
    if ops_file:
        try:
            with open(ops_file, encoding="utf-8") as f:
                ops = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            emit_error("OpsFileError", str(exc), exit_code=2)
            return []  # unreachable
    else:
        ops = parse_json_arg(ops_str, "ops")
    if not isinstance(ops, list) or not ops:
        emit_error(
            "ValueError",
            "ops must be a non-empty JSON array of {op: <name>, ...} dicts",
            exit_code=2,
        )
    return ops


def _parse_image_mappings(raw: list[str]) -> dict[str, str]:
    """Parse repeated --image local_id=/abs/path/to/img.png args."""
    out: dict[str, str] = {}
    for entry in raw:
        if "=" not in entry:
            emit_error(
                "ValueError",
                f"--image expects 'local_id=/abs/path', got {entry!r}",
                exit_code=2,
            )
        local_id, path = entry.split("=", 1)
        local_id = local_id.strip()
        path = path.strip()
        if not local_id or not path:
            emit_error("ValueError", f"--image entry malformed: {entry!r}", exit_code=2)
        if not Path(path).is_file():
            emit_error(
                "FileNotFound",
                f"--image source not found: {path}",
                exit_code=2,
            )
        out[local_id] = path
    return out


# Short position aliases that the SKILL.md teaches the LLM to use, mapped to
# the long names the engine expects. Doing the translation here (instead of in
# the engine itself) keeps the skill self-contained and avoids touching code
# paths that the MCP layer used to share.
_POSITION_ALIASES = {
    "before": "before_paragraph",
    "after":  "after_paragraph",
}


def _normalize_positions(ops: list[dict]) -> list[dict]:
    """Translate friendly short position names ("before"/"after") to the
    long forms the underlying editor accepts. Ops that don't use position
    are passed through untouched."""
    out: list[dict] = []
    for op in ops:
        if isinstance(op, dict) and isinstance(op.get("position"), str):
            short = op["position"]
            if short in _POSITION_ALIASES:
                new_op = dict(op)
                new_op["position"] = _POSITION_ALIASES[short]
                out.append(new_op)
                continue
        out.append(op)
    return out


def _materialize_image_ops(
    ops: list[dict], image_paths: dict[str, str], workdir: Path
) -> list[dict]:
    """Stage image files into workdir and rewrite ops to reference local names.

    Each insert_image op's ``image_file_id`` is treated as a key into the
    --image mapping. The file is copied into workdir under a workdir-local name
    (e.g. ``image_chart1.png``) and the op's ``image_file_id`` is rewritten to
    that local name so the engine resolves it via its workdir-relative loader.
    """
    rewritten: list[dict] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            rewritten.append(op)
            continue
        if op.get("op") != "insert_image":
            rewritten.append(op)
            continue
        # Preferred path: op carries an explicit ``image_path`` (a sandbox path
        # the caller already sandbox_put the image to). The engine reads it
        # directly — no --image mapping, no staging here.
        if op.get("image_path"):
            rewritten.append(op)
            continue
        local_id = op.get("image_file_id")
        if not isinstance(local_id, str) or not local_id:
            emit_error(
                "ValueError",
                f"ops[{i}] insert_image: provide 'image_path' (a sandbox path, "
                f"e.g. /workspace/chart.png) or 'image_file_id' + a matching "
                f"--image mapping",
                exit_code=2,
            )
        if local_id not in image_paths:
            emit_error(
                "ValueError",
                f"ops[{i}] insert_image: image_file_id={local_id!r} not in --image mappings; "
                f"available: {list(image_paths)}",
                exit_code=2,
            )
        src = Path(image_paths[local_id])
        workdir_name = f"img_{local_id}{src.suffix or '.png'}"
        shutil.copy2(src, workdir / workdir_name)
        new_op = dict(op)
        new_op["image_filename"] = workdir_name  # what the engine expects
        new_op.pop("image_file_id", None)
        rewritten.append(new_op)
    return rewritten


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--input", required=True, help="source .docx path")
    p.add_argument("--output", required=True, help="output .docx path")
    p.add_argument("--ops", help="JSON array of ops, inline")
    p.add_argument("--ops-file", help="path to JSON file with ops array")
    p.add_argument(
        "--stop-on-error",
        action="store_true",
        help="abort the batch on first failing op (default: keep going, "
             "save partial result, report per-op failures in result.results[])",
    )
    p.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="LOCAL_ID=/abs/path/to/image.png",
        help="image mapping for insert_image ops; can be repeated",
    )
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    ops = _load_ops(args.ops, args.ops_file)
    image_paths = _parse_image_mappings(args.image)

    from engine.editor import apply_edits  # type: ignore

    final_name = Path(args.output).name

    try:
        with staged_workdir(
            {"input.docx": args.input},
            output_name=final_name,
            output_dst=args.output,
        ) as workdir:
            ops_normalized = _normalize_positions(ops)
            ops_final = _materialize_image_ops(ops_normalized, image_paths, workdir)
            result = apply_edits(
                input_filename="input.docx",
                output_filename=final_name,
                ops=ops_final,
                stop_on_error=args.stop_on_error,
            )
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))
        return

    # apply_edits returns {"results": [...], "ops_total", "ops_succeeded", ...}
    payload = {
        "ok": True,
        "meta": {
            "output": args.output,
            **(result if isinstance(result, dict) else {"raw": result}),
        },
    }
    # Surface ops_failed > 0 as a hint (still ok=true at script level so the
    # caller gets the per-op breakdown).
    if isinstance(result, dict) and result.get("ops_failed", 0) > 0:
        payload["meta"]["warning"] = (
            f"{result['ops_failed']} of {result.get('ops_total', '?')} "
            "op(s) failed; see results[] for per-op error details"
        )
    emit_json(payload)


if __name__ == "__main__":
    main()
