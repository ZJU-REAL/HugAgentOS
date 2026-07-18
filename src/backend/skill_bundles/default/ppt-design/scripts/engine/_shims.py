"""Vendored filesystem + libreoffice helpers for the ppt-design engine.

A tiny self-contained copy of the workdir / input-path / output-path
resolution plus the LibreOffice ``to_pdf`` helper, so the engine modules
stay fully self-contained and don't reach across package boundaries.

Public surface:

  - ``use_workdir(path)``    — context manager: pin workdir for this thread
  - ``workdir()``            — resolve current workdir (thread-local → env → cwd)
  - ``input_path(name)``     — ``workdir() / name``; raises FileNotFoundError
  - ``output_path(name)``    — ``workdir() / name``
  - ``to_pdf(input_filename, output_filename)`` — docx/xlsx/pptx → pdf via
    LibreOffice headless
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


# ── workdir resolution ───────────────────────────────────────────────

_workdir_local = threading.local()


def _get_thread_local_workdir() -> Optional[str]:
    return getattr(_workdir_local, "value", None)


@contextmanager
def use_workdir(path: os.PathLike[str] | str) -> Iterator[Path]:
    """Pin the engine's workdir to ``path`` for the current thread.

    Restores the previous value on exit. Stacks safely. The CLI uses this to
    materialise the user's input file into a per-call temp directory before
    calling engine functions that read/write by bare filename.
    """
    resolved = Path(path).resolve()
    previous = getattr(_workdir_local, "value", None)
    _workdir_local.value = str(resolved)
    try:
        yield resolved
    finally:
        if previous is None:
            try:
                del _workdir_local.value
            except AttributeError:
                pass
        else:
            _workdir_local.value = previous


def workdir() -> Path:
    """Return the directory the engine should read inputs from + write outputs to."""
    local_override = _get_thread_local_workdir()
    if local_override:
        return Path(local_override).resolve()

    env_override = os.environ.get("OFFICE_LIB_WORKDIR")
    if env_override:
        return Path(env_override).resolve()

    workspace = Path("/workspace")
    if workspace.is_dir():
        cwd = Path.cwd().resolve()
        ws_resolved = workspace.resolve()
        try:
            cwd.relative_to(ws_resolved)
        except ValueError:
            return ws_resolved
        return cwd

    return Path.cwd().resolve()


def input_path(name: str) -> Path:
    """Resolve an input filename to its on-disk path.

    Raises FileNotFoundError if the file is not present.
    """
    p = workdir() / name
    if not p.is_file():
        raise FileNotFoundError(
            f"input file '{name}' not found in workdir {workdir()}; "
            "the CLI is expected to copy the input into the workdir before calling the engine"
        )
    return p


def output_path(name: str) -> Path:
    """Resolve an output filename within the active workdir."""
    return workdir() / name


# ── LibreOffice headless: pptx → pdf ────────────────────────────────

_SOFFICE_TIMEOUT_S = 90  # cold start ~5-10s, plus rendering


def _soffice_binary() -> str:
    for candidate in ("libreoffice", "soffice"):
        path = shutil.which(candidate)
        if path:
            return path
    raise FileNotFoundError(
        "LibreOffice not found in PATH; the mcp container image must include "
        "libreoffice-impress (see docker/Dockerfile.mcp)"
    )


def _find_java_home() -> str | None:
    """Locate a JRE under /usr/lib/jvm/ — LibreOffice Impress needs Java to import pptx."""
    if os.environ.get("JAVA_HOME") and os.path.isfile(
        os.path.join(os.environ["JAVA_HOME"], "bin", "java")
    ):
        return os.environ["JAVA_HOME"]
    jvm_root = Path("/usr/lib/jvm")
    if not jvm_root.is_dir():
        return None
    candidates = sorted(jvm_root.iterdir(), reverse=True)
    for c in candidates:
        if (c / "bin" / "java").is_file():
            return str(c)
    return None


def to_pdf(*, input_filename: str, output_filename: str) -> dict[str, Any]:
    """Convert a pptx (or docx/xlsx) in workdir to a PDF (also in workdir).

    Used by ``thumbnails.render_thumbnails`` (pptx → pdf → jpgs) and by the
    CLI's ``to-pdf`` subcommand.
    """
    src = input_path(input_filename)
    out = output_path(output_filename)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not output_filename.lower().endswith(".pdf"):
        raise ValueError(f"output_filename must end with '.pdf', got {output_filename!r}")

    soffice = _soffice_binary()

    env = os.environ.copy()
    if not env.get("JAVA_HOME"):
        jh = _find_java_home()
        if jh:
            env["JAVA_HOME"] = jh
            env["PATH"] = jh + "/bin:" + env.get("PATH", "")

    with tempfile.TemporaryDirectory(prefix="lo_pdf_") as tmp:
        cmd = [
            soffice,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            tmp,
            str(src),
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_SOFFICE_TIMEOUT_S,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"LibreOffice conversion timed out after {_SOFFICE_TIMEOUT_S}s"
            ) from exc

        produced = Path(tmp) / (src.stem + ".pdf")
        if res.returncode != 0 or not produced.is_file():
            tail = (res.stderr or res.stdout or "").strip()[-500:]
            raise RuntimeError(
                f"LibreOffice exited {res.returncode}; produced PDF "
                f"{'present' if produced.is_file() else 'missing'}. tail: {tail}"
            )

        produced.replace(out)

    size = out.stat().st_size

    pages: int | None = None
    try:
        from pypdf import PdfReader

        pages = len(PdfReader(str(out)).pages)
    except Exception:
        pass

    return {
        "output_filename": output_filename,
        "size_bytes": size,
        "pages": pages,
    }
