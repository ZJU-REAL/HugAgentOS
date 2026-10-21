"""Shared utilities for the pdf-editing skill scripts.

Every script imports from here to ensure:
- the vendored ``engine`` package is importable regardless of cwd (we add
  this ``scripts/`` directory to ``sys.path`` automatically)
- temp workdir / input staging / output extraction is uniform
- stdout is a single line of JSON every invocation, so the LLM caller can
  parse with one ``jq`` / ``json.loads`` and not worry about logging noise

Public surface:
    setup_path()          — call once at import; injects sys.path
    staged_workdir(...)   — context manager that stages inputs + extracts output
    emit_json(...)        — print final JSON + exit
    emit_error(...)       — print {ok:false, error:{...}} + exit nonzero
    parse_json_arg(...)   — decode a JSON CLI arg with a clean error
    load_json_arg_or_file(...) — accept either inline JSON or a JSON file path
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def setup_path() -> None:
    """Make the vendored ``engine`` package importable from any cwd.

    The skill scripts run as ``python <scripts>/cli.py`` (or a subscript),
    so ``scripts/`` is normally already ``sys.path[0]``. We add it explicitly
    anyway so ``import engine`` also works when a script is imported as a
    module (e.g. from a test harness) rather than executed directly.
    """
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


setup_path()


@contextmanager
def staged_workdir(
    inputs: dict[str, str] | None,
    *,
    output_name: str | None = None,
    output_dst: str | None = None,
) -> Iterator[Path]:
    """Stage input files into a fresh temp workdir, run the body, extract output."""
    if output_dst is not None and not output_name:
        raise ValueError("output_dst requires output_name")

    with tempfile.TemporaryDirectory(prefix="pdf_skill_") as wd_str:
        workdir = Path(wd_str)
        for basename, src in (inputs or {}).items():
            src_path = Path(src)
            if not src_path.is_file():
                raise FileNotFoundError(f"input file not found: {src}")
            shutil.copy2(src_path, workdir / basename)

        prev_workdir = os.environ.get("OFFICE_LIB_WORKDIR")
        os.environ["OFFICE_LIB_WORKDIR"] = str(workdir)
        try:
            yield workdir
        finally:
            if prev_workdir is None:
                os.environ.pop("OFFICE_LIB_WORKDIR", None)
            else:
                os.environ["OFFICE_LIB_WORKDIR"] = prev_workdir

        if output_dst is not None:
            produced = workdir / output_name  # type: ignore[arg-type]
            if not produced.is_file():
                raise FileNotFoundError(
                    f"expected output '{output_name}' was not produced in workdir"
                )
            dst = Path(output_dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(produced, dst)


def emit_json(payload: dict[str, Any], *, exit_code: int = 0) -> None:
    """Print a single-line JSON response to stdout and exit."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(exit_code)


def emit_error(
    kind: str,
    message: str,
    *,
    exit_code: int = 1,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"type": kind, "message": message},
    }
    if extra:
        payload["error"].update(extra)
    emit_json(payload, exit_code=exit_code)


def parse_json_arg(raw: str | None, field_name: str) -> Any:
    """Decode a JSON-string CLI arg, raising a clean error on bad input."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        emit_error(
            "InvalidJSONArg",
            f"--{field_name} must be valid JSON: {exc}",
            exit_code=2,
        )
        return None  # unreachable; emit_error exits


def load_json_arg_or_file(
    raw_arg: str | None, file_arg: str | None, field_name: str
) -> Any:
    """Load JSON from either ``--<field> <inline>`` or ``--<field>-file <path>``."""
    if (raw_arg is None) == (file_arg is None):
        emit_error(
            "ValueError",
            f"exactly one of --{field_name} or --{field_name}-file must be provided",
            exit_code=2,
        )
    if file_arg:
        try:
            with open(file_arg, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            emit_error(
                "InvalidJSONArg",
                f"--{field_name}-file: {exc}",
                exit_code=2,
            )
    return parse_json_arg(raw_arg, field_name)


def staged_workdir_multi_inputs(
    inputs: list[tuple[str, str]],
) -> "_MultiInputCtx":
    """Stage N input files for tools that take a variable list (e.g. merge).

    Returns a context manager that yields the workdir Path. Inputs is a list
    of ``(basename_in_workdir, abs_source_path)`` tuples.
    """
    return _MultiInputCtx(inputs)


class _MultiInputCtx:
    def __init__(self, inputs: list[tuple[str, str]]):
        self.inputs = inputs
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._prev_workdir: str | None = None

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf_skill_")
        workdir = Path(self._tmp.name)
        for basename, src in self.inputs:
            src_path = Path(src)
            if not src_path.is_file():
                raise FileNotFoundError(f"input file not found: {src}")
            shutil.copy2(src_path, workdir / basename)
        self._prev_workdir = os.environ.get("OFFICE_LIB_WORKDIR")
        os.environ["OFFICE_LIB_WORKDIR"] = str(workdir)
        return workdir

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._prev_workdir is None:
            os.environ.pop("OFFICE_LIB_WORKDIR", None)
        else:
            os.environ["OFFICE_LIB_WORKDIR"] = self._prev_workdir
        if self._tmp is not None:
            self._tmp.cleanup()


def _refuse_direct_run() -> None:
    """Fail loudly when ``_common.py`` is executed as a script.

    ``_common.py`` is a shared library — every subcommand imports it, none
    *is* it. Without this guard, ``python scripts/_common.py pdf-cli
    create …`` was a silent no-op: Python imported the module, defined these
    functions, and exited 0 with empty stdout. The caller saw "success" with
    no file produced and no error to react to — the exact failure mode that
    sends an agent into a retry loop. So instead: emit a structured error and
    reconstruct the command the caller almost certainly meant.
    """
    argv = sys.argv[1:]
    # Drop a redundant dispatcher token if one leaked in, e.g.
    # ``python _common.py pdf-cli create …`` → suggest ``pdf-cli create …``.
    while argv and argv[0] in ("pdf-cli", "cli.py", "cli", "_common.py"):
        argv = argv[1:]
    suggested = "pdf-cli " + " ".join(argv) if argv else "pdf-cli <subcommand> [args]"
    emit_error(
        "NotAnEntryPoint",
        "_common.py is a shared library for the pdf-editing skill, not a "
        "runnable command — nothing was executed and no file was produced. "
        "`pdf-cli` is already on PATH; never run `python scripts/*.py` "
        f"directly. Re-run as: {suggested}",
        exit_code=2,
        extra={"received_argv": sys.argv[1:], "suggested_command": suggested},
    )


if __name__ == "__main__":
    _refuse_direct_run()
