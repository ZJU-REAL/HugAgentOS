#!/usr/bin/env python3
"""merge.py — concatenate multiple PDFs into a single PDF, in given order.

Wraps ``engine.merger.merge``. Inputs must all be local .pdf files
(send them into the sandbox via ``sandbox_put_artifact`` first if they live
in artifact storage).

Output: a single JSON line to stdout. exit 0 on success, 1 on business error,
2 on argparse error.

Usage:
    merge.py --output /workspace/merged.pdf --inputs /workspace/a.pdf /workspace/b.pdf /workspace/c.pdf
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from _common import (
    emit_error,
    emit_json,
    staged_workdir_multi_inputs,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", required=True, help="path to write the merged .pdf")
    p.add_argument(
        "--inputs",
        required=True,
        nargs="+",
        help="ordered list of input .pdf paths (≥ 2)",
    )
    args = p.parse_args()

    if len(args.inputs) < 2:
        emit_error(
            "ValueError",
            f"merge needs at least 2 input PDFs (got {len(args.inputs)})",
            exit_code=2,
        )

    for src in args.inputs:
        if not Path(src).is_file():
            emit_error("FileNotFound", f"input not found: {src}", exit_code=2)

    out_path = args.output if args.output.lower().endswith(".pdf") else args.output + ".pdf"

    # Stage all inputs as input_00.pdf, input_01.pdf, ...
    inputs: list[tuple[str, str]] = [
        (f"input_{i:02d}.pdf", src) for i, src in enumerate(args.inputs)
    ]
    input_filenames = [name for name, _ in inputs]
    output_basename = "merged.pdf"

    from engine.merger import merge  # type: ignore

    try:
        with staged_workdir_multi_inputs(inputs) as workdir:
            result = merge(
                input_filenames=input_filenames,
                output_filename=output_basename,
            )
            produced = workdir / output_basename
            if not produced.is_file():
                emit_error("OutputMissing", f"merger did not produce {output_basename}")
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(produced, out_path)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))

    emit_json({"ok": True, "meta": result})


if __name__ == "__main__":
    main()
