"""kb parser state responsibilities."""
from __future__ import annotations
import re

MIME_TO_SUFFIX: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "text/plain": ".txt",
    "text/markdown": ".txt",
    "text/x-markdown": ".txt",
    "text/csv": ".csv",
    "application/json": ".txt",
}


MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


IMAGE_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


HEADING_SOFT_RATIO = 0.5


RECURSIVE_SEPARATORS = ["\n\n", "\n", "。", ".", "；", ";", "，", ",", " "]


SEPARATOR_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\r": "\r", "\\\\": "\\"}
