"""Shared utilities for the word-editing skill scripts.

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
    run_dotnet(...)       — invoke the bundled .NET CLI synchronously
    DOTNET_DLL / ASSETS_DIR — resolved paths to the .NET CLI and its assets
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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


DOTNET_DLL = os.environ.get(
    "MINIMAX_DOCX_BIN", "/opt/minimax-docx/MiniMaxAIDocx.Cli.dll"
)
ASSETS_DIR = os.environ.get(
    "MINIMAX_DOCX_ASSETS", "/opt/minimax-docx/assets"
)


@contextmanager
def staged_workdir(
    inputs: dict[str, str],
    *,
    output_name: str | None = None,
    output_dst: str | None = None,
) -> Iterator[Path]:
    """Stage input files into a fresh temp workdir, run the body, extract output.

    Args:
        inputs: ``{basename_in_workdir: absolute_source_path}``. Each source
            is copied into the workdir under the given basename. The body
            receives the workdir path; functions that consume the ``engine``
            should pass basenames as ``input_filename`` / ``output_filename``.
        output_name: basename of the expected output file inside the workdir.
            Required when ``output_dst`` is set.
        output_dst: absolute path to copy the produced output into when the
            body exits successfully. Skip if the operation produces no file
            (read-only modes).

    The context manager also pins ``OFFICE_LIB_WORKDIR`` to the workdir, so
    ``engine._handle.input_path / output_path`` resolve against it.
    """
    if output_dst is not None and not output_name:
        raise ValueError("output_dst requires output_name")

    with tempfile.TemporaryDirectory(prefix="word_skill_") as wd_str:
        workdir = Path(wd_str)
        for basename, src in inputs.items():
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


def run_dotnet(
    subcommand: str,
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: int = 90,
) -> subprocess.CompletedProcess[str]:
    """Invoke the bundled .NET CLI synchronously.

    Args:
        subcommand: one of ``create / edit / apply-template / validate /
            merge-runs / fix-order / analyze / diff``.
        args: additional CLI args (e.g. ``["--input", "in.docx", "--json"]``).
        cwd: working directory for the subprocess; defaults to current.
        timeout: seconds before raising TimeoutExpired.

    Returns the CompletedProcess. Caller checks ``returncode`` / ``stdout`` /
    ``stderr``. Does NOT print anything itself.

    Raises ``FileNotFoundError`` if the .NET runtime or CLI dll is missing.
    """
    if not shutil.which("dotnet"):
        raise FileNotFoundError(
            "'dotnet' not found in PATH; container must install dotnet-runtime-8.0"
        )
    if not os.path.isfile(DOTNET_DLL):
        raise FileNotFoundError(
            f"minimax-docx CLI dll not found at {DOTNET_DLL!r}; "
            "set MINIMAX_DOCX_BIN or rebuild the mcp container"
        )

    cmd = ["dotnet", DOTNET_DLL, subcommand, *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


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
