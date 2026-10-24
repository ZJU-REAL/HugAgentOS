"""File extensions the parsers accept."""
from __future__ import annotations


EXT_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc_wps",
    ".wps": "doc_wps",
    ".txt": "txt",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".csv": "csv",
    ".pptx": "pptx",
}


RAW_TEXT_EXTS = {
    ".html", ".htm",
    ".md", ".markdown",
    ".json", ".jsonl", ".ndjson",
    ".yaml", ".yml",
    ".toml", ".ini", ".cfg",
    ".xml", ".svg",
    ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash",
    ".css", ".scss",
    ".tex", ".rst",
    ".sql",
}


SUPPORTED_EXTENSIONS = list(EXT_MAP.keys()) + sorted(RAW_TEXT_EXTS)
