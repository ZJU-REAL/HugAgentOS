#!/usr/bin/env python3
"""validate.py — XSD + business-rule validation, optionally with auto-repair.

Use when:
    - After any non-trivial edit, before delivering, to catch corruption
      (required after apply_template.py per minimax-docx best practice)
    - Gate-checking that a generated doc matches a reference template's
      structure ("did we produce all 5 required sections?")
    - Recovering a doc that opens but produces validation warnings in Word

Modes:
    no flag:                XSD + business rules only, no output file
    --repair --output X:    merge-runs → fix-order → validate, writes X
    --gate-check T:         additionally diff against template T's structure

Usage examples:
    validate.py --input out.docx
    validate.py --input out.docx --repair --output out.fixed.docx
    validate.py --input out.docx --gate-check template.docx
    validate.py --input out.docx --no-xsd-check --business-rules
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import (
    ASSETS_DIR,
    emit_error,
    emit_json,
    run_dotnet,
    staged_workdir,
)


def _parse_validate_stdout(stdout: str) -> dict:
    """Parse the .NET validate --json output."""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {"_parse_error": str(exc), "_raw_tail": stdout[-500:]}


def _validate_only(
    *,
    input_path: str,
    xsd_check: bool,
    business_rules: bool,
    gate_check_path: str | None,
) -> None:
    inputs: dict[str, str] = {"doc.docx": input_path}
    cli_args = ["--input", "doc.docx", "--json"]
    if xsd_check:
        cli_args += ["--xsd", f"{ASSETS_DIR}/xsd/wml-subset.xsd"]
    if business_rules:
        cli_args += ["--business"]
    if gate_check_path:
        inputs["gate.docx"] = gate_check_path
        cli_args += ["--gate-check", "gate.docx"]

    with staged_workdir(inputs) as workdir:
        proc = run_dotnet("validate", cli_args, cwd=workdir, timeout=60)

    # exit 0 = valid, 1 = invalid but well-formed report, other = hard failure
    if proc.returncode not in (0, 1):
        emit_error(
            "DotnetError",
            f"validate hard-failed (exit {proc.returncode})",
            extra={"stdout": proc.stdout, "stderr": proc.stderr},
        )

    report = _parse_validate_stdout(proc.stdout)
    meta: dict = {
        "is_valid": bool(report.get("isValid")) if isinstance(report, dict) else False,
        "errors": report.get("errors", []) if isinstance(report, dict) else [],
        "warnings": report.get("warnings", []) if isinstance(report, dict) else [],
        "exit_code": proc.returncode,
    }
    if isinstance(report, dict) and report.get("gateCheck") is not None:
        meta["gate_check"] = report["gateCheck"]
    if isinstance(report, dict) and "_parse_error" in report:
        meta["parse_error"] = report["_parse_error"]
    emit_json({"ok": True, "meta": meta})


def _validate_with_repair(
    *,
    input_path: str,
    output_path: str,
    xsd_check: bool,
    business_rules: bool,
    gate_check_path: str | None,
) -> None:
    """Pipeline: merge-runs → fix-order → validate. All run in one workdir."""
    inputs: dict[str, str] = {"in.docx": input_path}
    if gate_check_path:
        inputs["gate.docx"] = gate_check_path
    final_name = Path(output_path).name

    with staged_workdir(
        inputs,
        output_name=final_name,
        output_dst=output_path,
    ) as workdir:
        # Step 1: merge-runs (consolidate adjacent runs with identical formatting)
        proc1 = run_dotnet(
            "merge-runs",
            ["--input", "in.docx", "--output", "merged.docx"],
            cwd=workdir,
            timeout=60,
        )
        if proc1.returncode != 0:
            emit_error(
                "DotnetError",
                f"merge-runs failed (exit {proc1.returncode})",
                extra={"stdout": proc1.stdout, "stderr": proc1.stderr},
            )

        # Step 2: fix-order (reorder OOXML element children per ISO 29500)
        proc2 = run_dotnet(
            "fix-order",
            ["--input", "merged.docx", "--output", final_name],
            cwd=workdir,
            timeout=60,
        )
        if proc2.returncode != 0:
            emit_error(
                "DotnetError",
                f"fix-order failed (exit {proc2.returncode})",
                extra={"stdout": proc2.stdout, "stderr": proc2.stderr},
            )

        # Step 3: validate the repaired output
        validate_args = ["--input", final_name, "--json"]
        if xsd_check:
            validate_args += ["--xsd", f"{ASSETS_DIR}/xsd/wml-subset.xsd"]
        if business_rules:
            validate_args += ["--business"]
        if gate_check_path:
            validate_args += ["--gate-check", "gate.docx"]
        proc3 = run_dotnet("validate", validate_args, cwd=workdir, timeout=60)
        if proc3.returncode not in (0, 1):
            emit_error(
                "DotnetError",
                f"post-repair validate hard-failed (exit {proc3.returncode})",
                extra={"stdout": proc3.stdout, "stderr": proc3.stderr},
            )

    report = _parse_validate_stdout(proc3.stdout)
    meta: dict = {
        "output": output_path,
        "repairs_applied": ["merge-runs", "fix-order"],
        "is_valid": bool(report.get("isValid")) if isinstance(report, dict) else False,
        "errors": report.get("errors", []) if isinstance(report, dict) else [],
        "warnings": report.get("warnings", []) if isinstance(report, dict) else [],
        "exit_code": proc3.returncode,
    }
    if isinstance(report, dict) and report.get("gateCheck") is not None:
        meta["gate_check"] = report["gateCheck"]
    emit_json({"ok": True, "meta": meta})


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--input", required=True)
    p.add_argument("--output", help="required when --repair is set")
    p.add_argument("--repair", action="store_true",
                   help="run merge-runs + fix-order BEFORE validate; produces a new file")

    p.add_argument("--xsd-check", dest="xsd_check", action="store_true", default=True)
    p.add_argument("--no-xsd-check", dest="xsd_check", action="store_false")
    p.add_argument("--business-rules", dest="business_rules", action="store_true", default=True)
    p.add_argument("--no-business-rules", dest="business_rules", action="store_false")

    p.add_argument("--gate-check", help="path to reference template; compare structure")
    args = p.parse_args()

    if not Path(args.input).is_file():
        emit_error("FileNotFound", f"input not found: {args.input}", exit_code=2)
    if args.gate_check and not Path(args.gate_check).is_file():
        emit_error("FileNotFound", f"--gate-check not found: {args.gate_check}", exit_code=2)
    if args.repair and not args.output:
        emit_error("ValueError", "--repair requires --output", exit_code=2)
    if not args.xsd_check and not args.business_rules and not args.gate_check:
        emit_error(
            "ValueError",
            "all checks disabled — at least one of xsd/business/gate-check must run",
            exit_code=2,
        )

    try:
        if args.repair:
            _validate_with_repair(
                input_path=args.input,
                output_path=args.output,
                xsd_check=args.xsd_check,
                business_rules=args.business_rules,
                gate_check_path=args.gate_check,
            )
        else:
            _validate_only(
                input_path=args.input,
                xsd_check=args.xsd_check,
                business_rules=args.business_rules,
                gate_check_path=args.gate_check,
            )
    except FileNotFoundError as exc:
        emit_error("RuntimeMissing", str(exc))
    except Exception as exc:  # noqa: BLE001
        emit_error(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
