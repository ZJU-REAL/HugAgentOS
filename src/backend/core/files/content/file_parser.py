"""Dispatch an uploaded file to the parser for its extension."""
from __future__ import annotations
import os
from typing import Optional
from core.files.content import file_formats
from core.files.content import pdf_parser
from core.files.content import presentation_parser
from core.files.content import spreadsheet_parser
from core.files.content import text_parser
from core.files.content import word_parser

def parse_file(file_bytes: bytes, filename: str) -> Optional[str]:
    """
    Dispatch to the correct parser by file extension.

    Returns extracted text/markdown, or None if the format is unsupported.
    Raises RuntimeError with a Chinese message on parse failure.
    """
    suffix = os.path.splitext(filename.lower())[1]
    kind = file_formats.EXT_MAP.get(suffix)

    if kind == "pdf":
        return pdf_parser.parse_pdf(file_bytes, filename)
    elif kind == "docx":
        return word_parser.parse_docx(file_bytes)
    elif kind == "doc_wps":
        return word_parser.parse_doc_wps(file_bytes, suffix)
    elif kind == "txt":
        return text_parser.parse_txt(file_bytes)
    elif kind == "xlsx":
        return spreadsheet_parser.parse_xlsx(file_bytes)
    elif kind == "xls":
        return spreadsheet_parser.parse_xls(file_bytes)
    elif kind == "csv":
        return spreadsheet_parser.parse_csv(file_bytes)
    elif kind == "pptx":
        return presentation_parser.parse_pptx(file_bytes)
    elif suffix in file_formats.RAW_TEXT_EXTS:
        return text_parser.parse_raw_text(file_bytes)
    else:
        return None
