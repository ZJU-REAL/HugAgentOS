"""docx/xlsx/pptx → PDF conversion via LibreOffice headless.

The sandbox image (script-runner or opensandbox-custom) installs
``libreoffice-writer/-calc/-impress``. LibreOffice's
``soffice --headless --convert-to pdf`` is the standard portable way to render
Office files to PDF without a Microsoft stack.

Java requirement: LibreOffice's xlsx (Calc) and pptx (Impress) import paths
need a JRE — without ``JAVA_HOME`` resolvable, conversion silently fails with
``Warning: failed to launch javaldx`` + ``Error: source file could not be loaded``.
We auto-detect a JRE under ``/usr/lib/jvm/`` and export ``JAVA_HOME`` for the
subprocess so the convert succeeds even when the kernel's environment doesn't
set it (observed on opensandbox/code-interpreter:v1.0.2-derived images).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ._handle import input_path, output_path

_SOFFICE_TIMEOUT_S = 90  # LibreOffice cold start ~5-10s, plus rendering


def _soffice_binary() -> str:
    """Locate the LibreOffice binary; raise FileNotFoundError with a hint if missing."""
    for candidate in ("libreoffice", "soffice"):
        path = shutil.which(candidate)
        if path:
            return path
    raise FileNotFoundError(
        "LibreOffice not found in PATH; sandbox image must include libreoffice-writer "
        "(or libreoffice-core for full suite)"
    )


def _find_java_home() -> str | None:
    """Locate a JRE under ``/usr/lib/jvm/`` and return its install root.

    Returns None if no JRE is found — caller can decide whether to proceed
    (works fine for plain docx) or fail (needed for xlsx/pptx).
    """
    if os.environ.get("JAVA_HOME") and os.path.isfile(
        os.path.join(os.environ["JAVA_HOME"], "bin", "java")
    ):
        return os.environ["JAVA_HOME"]
    jvm_root = Path("/usr/lib/jvm")
    if not jvm_root.is_dir():
        return None
    # Prefer newer JDK over older; default-java symlink wins if present
    candidates = sorted(jvm_root.iterdir(), reverse=True)
    for c in candidates:
        java_bin = c / "bin" / "java"
        if java_bin.is_file():
            return str(c)
    return None


def to_pdf(
    *,
    input_filename: str,
    output_filename: str,
) -> dict[str, Any]:
    """Convert a docx/xlsx/pptx in cwd to a PDF (also in cwd).

    Args:
        input_filename: source filename — extension determines the LibreOffice
            filter (.docx / .xlsx / .pptx)
        output_filename: target PDF filename (must end ``.pdf``)

    Returns:
        ``{"output_filename", "size_bytes", "pages": int?}`` (page count
        is best-effort: counted via pypdf if the import succeeds)
    """
    src = input_path(input_filename)
    out = output_path(output_filename)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not output_filename.lower().endswith(".pdf"):
        raise ValueError(f"output_filename must end with '.pdf', got {output_filename!r}")

    soffice = _soffice_binary()

    # Build env: ensure JAVA_HOME points at a real JRE, otherwise xlsx/pptx
    # import paths fail silently with "source file could not be loaded".
    env = os.environ.copy()
    if not env.get("JAVA_HOME"):
        jh = _find_java_home()
        if jh:
            env["JAVA_HOME"] = jh
            env["PATH"] = jh + "/bin:" + env.get("PATH", "")

    # LibreOffice writes <input_basename>.pdf into --outdir; we then rename to the requested name.
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

    # Page count is nice-to-have; failure should not break the conversion result.
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


def to_docx(
    *,
    input_filename: str,
    output_filename: str,
) -> dict[str, Any]:
    """Convert a legacy .doc (Word 97-2003 binary) to .docx via LibreOffice.

    Newer .docx inputs are accepted (a no-op convert) but the typical use case
    is upgrading a binary .doc that python-docx can't read into a modern .docx.

    Args:
        input_filename: source filename in cwd (typically ``.doc`` or ``.rtf``).
        output_filename: target filename in cwd; must end ``.docx``.

    Returns:
        ``{"output_filename", "size_bytes"}``
    """
    src = input_path(input_filename)
    out = output_path(output_filename)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not output_filename.lower().endswith(".docx"):
        raise ValueError(f"output_filename must end with '.docx', got {output_filename!r}")

    soffice = _soffice_binary()

    # Plain docx conversion doesn't need Java, but xlsx/pptx fallback does;
    # we set JAVA_HOME defensively in case the input is something else .doc-ish.
    env = os.environ.copy()
    if not env.get("JAVA_HOME"):
        jh = _find_java_home()
        if jh:
            env["JAVA_HOME"] = jh
            env["PATH"] = jh + "/bin:" + env.get("PATH", "")

    # LibreOffice writes <input_basename>.docx into --outdir; we then rename.
    with tempfile.TemporaryDirectory(prefix="lo_docx_") as tmp:
        cmd = [
            soffice,
            "--headless",
            "--convert-to",
            "docx",
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

        produced = Path(tmp) / (src.stem + ".docx")
        if res.returncode != 0 or not produced.is_file():
            tail = (res.stderr or res.stdout or "").strip()[-500:]
            raise RuntimeError(
                f"LibreOffice exited {res.returncode}; produced docx "
                f"{'present' if produced.is_file() else 'missing'}. tail: {tail}"
            )

        produced.replace(out)

    size = out.stat().st_size
    return {
        "output_filename": output_filename,
        "size_bytes": size,
    }
