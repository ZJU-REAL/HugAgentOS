"""Decode plain-text files."""
from __future__ import annotations


def parse_txt(file_bytes: bytes) -> str:
    """Decode text file bytes, trying UTF-8 then GBK then latin-1."""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


def parse_raw_text(file_bytes: bytes) -> str:
    """UTF-8 decode for plain-text formats (HTML/MD/code/etc.).

    No structural extraction—just the source bytes as a string. Replaces
    undecodable bytes so we always return something usable.
    """
    return file_bytes.decode("utf-8", errors="replace")
