"""Parse DOCX, DOC and WPS files into markdown."""
from __future__ import annotations
import os
import subprocess
import tempfile
from core.files.content.office import find_libreoffice_binary, find_pandoc_binary
from core.infra.proc import no_window_kwargs

def docx_bytes_to_markdown(docx_bytes: bytes) -> str:
    """Convert DOCX bytes to markdown string via pandoc."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "input.docx")
        with open(input_path, "wb") as f:
            f.write(docx_bytes)

        try:
            result = subprocess.run(
                [find_pandoc_binary() or "pandoc", input_path, "-t", "markdown", "--wrap=none"],
                capture_output=True,
                text=True,
                timeout=60,
                **no_window_kwargs(),
            )
        except FileNotFoundError:
            raise RuntimeError("pandoc 未安装，无法解析 Word 文档")
        except subprocess.TimeoutExpired:
            raise RuntimeError("pandoc 转换超时")

        if result.returncode != 0:
            raise RuntimeError(f"pandoc 转换失败: {result.stderr[:300]}")

        return result.stdout


def parse_docx(file_bytes: bytes) -> str:
    """Parse DOCX bytes → markdown via pandoc."""
    return docx_bytes_to_markdown(file_bytes)


def convert_to_docx_bytes(file_bytes: bytes, suffix: str) -> bytes:
    """Use LibreOffice headless to convert DOC/WPS bytes → DOCX bytes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        with open(input_path, "wb") as f:
            f.write(file_bytes)

        try:
            result = subprocess.run(
                [
                    find_libreoffice_binary() or "libreoffice",
                    "--headless",
                    "--convert-to", "docx",
                    "--outdir", tmpdir,
                    input_path,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                **no_window_kwargs(),
            )
        except FileNotFoundError:
            raise RuntimeError("LibreOffice 未安装，无法解析 DOC/WPS 文件")
        except subprocess.TimeoutExpired:
            raise RuntimeError("LibreOffice 转换超时")

        output_path = os.path.join(tmpdir, "input.docx")
        if not os.path.exists(output_path):
            raise RuntimeError(f"LibreOffice 转换失败: {result.stderr[:300]}")

        with open(output_path, "rb") as f:
            return f.read()


def parse_doc_wps(file_bytes: bytes, suffix: str) -> str:
    """Parse DOC/WPS via LibreOffice → DOCX → pandoc markdown."""
    docx_bytes = convert_to_docx_bytes(file_bytes, suffix)
    return docx_bytes_to_markdown(docx_bytes)
