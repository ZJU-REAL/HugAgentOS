#!/usr/bin/env python3
"""convert.py — file-format conversions via LibreOffice headless.

Two modes:
    --to docx   convert legacy .doc / .rtf / .odt → .docx
    --to pdf    render .docx → .pdf

Both modes shell out to ``soffice --headless --convert-to ...``. LibreOffice
must be installed (mcp container Dockerfile installs it).

Usage:
    convert.py --to docx --input old.doc --output new.docx
    convert.py --to pdf  --input draft.docx --output draft.pdf
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from _common import emit_error, emit_json, staged_workdir


def _run_soffice(target_format: str, src_basename: str, workdir: Path) -> tuple[int, str, str]:
    """Run ``soffice --headless --convert-to <fmt>`` inside workdir."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        emit_error(
            "RuntimeMissing",
            "'soffice' / 'libreoffice' not found in PATH; container must install LibreOffice",
        )
    cmd = [
        soffice,
        "--headless",
        "--convert-to", target_format,
        "--outdir", str(workdir),
        src_basename,
    ]
    proc = subprocess.run(
        cmd, cwd=str(workdir), capture_output=True, text=True, timeout=180
    )
    return proc.returncode, proc.stdout, proc.stderr


def _to_docx(input_path: str, output_path: str) -> None:
    src_basename = Path(input_path).name
    final_name = Path(output_path).name

    with staged_workdir(
        {src_basename: input_path},
        output_name=final_name,
        output_dst=output_path,
    ) as workdir:
        rc, out, err = _run_soffice("docx", src_basename, workdir)
        if rc != 0:
            emit_error(
                "LibreOfficeError",
                f"convert-to docx failed (exit {rc})",
                extra={"stdout": out, "stderr": err},
            )
        # soffice writes <basename-without-ext>.docx; rename to user's name if needed
        produced_default = workdir / (Path(src_basename).stem + ".docx")
        if produced_default.is_file() and produced_default.name != final_name:
            produced_default.rename(workdir / final_name)
        elif not (workdir / final_name).is_file():
            emit_error(
                "LibreOfficeError",
                f"expected output file not produced in workdir",
                extra={"workdir_contents": [p.name for p in workdir.iterdir()]},
            )

    emit_json({
        "ok": True,
        "meta": {"output": output_path, "format": "docx"},
    })


def _to_pdf(input_path: str, output_path: str) -> None:
    src_basename = Path(input_path).name
    final_name = Path(output_path).name

    with staged_workdir(
        {src_basename: input_path},
        output_name=final_name,
        output_dst=output_path,
    ) as workdir:
        rc, out, err = _run_soffice("pdf", src_basename, workdir)
        if rc != 0:
            emit_error(
                "LibreOfficeError",
                f"convert-to pdf failed (exit {rc})",
                extra={"stdout": out, "stderr": err},
            )
        produced_default = workdir / (Path(src_basename).stem + ".pdf")
        if produced_default.is_file() and produced_default.name != final_name:
            produced_default.rename(workdir / final_name)
        elif not (workdir / final_name).is_file():
            emit_error(
                "LibreOfficeError",
                "expected pdf not produced in workdir",
                extra={"workdir_contents": [p.name for p in workdir.iterdir()]},
            )

    # Best-effort page count (pdftotext or pdfinfo not always present; skip if missing)
    pages = None
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        try:
            info_proc = subprocess.run(
                [pdfinfo, output_path], capture_output=True, text=True, timeout=10
            )
            for line in info_proc.stdout.splitlines():
                if line.startswith("Pages:"):
                    pages = int(line.split(":", 1)[1].strip())
                    break
        except Exception:  # noqa: BLE001
            pass

    meta = {"output": output_path, "format": "pdf"}
    if pages is not None:
        meta["pages"] = pages
    emit_json({"ok": True, "meta": meta})


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--to", dest="target", required=True, choices=["docx", "pdf"])
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)

    expected_ext = "." + args.target
    if not args.output.endswith(expected_ext):
        args.output += expected_ext

    try:
        if args.target == "docx":
            _to_docx(args.input, args.output)
        else:
            _to_pdf(args.input, args.output)
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
