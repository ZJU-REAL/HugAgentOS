"""Binary file support for DB skills' text-only ``extra_files`` storage.

``AdminSkill.extra_files`` is a JSONB map of ``{relative_path: str}`` — UTF-8
text only, because JSONB can't hold raw bytes (and Postgres rejects NUL). To let
a skill ship binary assets (PNG logos, fonts, .pptx templates, …) we store them
as a base64 string flagged with a sentinel prefix, and decode them back to bytes
when materializing the skill to disk.

The sentinel is namespaced so it can't collide with real text-file content. Both
the writer (``api/routes/v1/admin_skills.py`` upload) and the reader
(``agent_skills/loader.py`` materialize) import these helpers so the encoding
stays in one place.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Dict, Iterable

# Prefix that flags an extra_files value as base64-encoded binary. Chosen to be
# vanishingly unlikely to appear at the start of a genuine text file.
BINARY_MARKER = "__JX_BINARY_BASE64__:"

# Shared storage policy for skill/plugin ``extra_files`` packing (single source —
# imported by marketplace_service, plugin_importer, admin upload).
MAX_SINGLE_FILE = 100 * 1024 * 1024
MAX_TOTAL = 150 * 1024 * 1024
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".wav", ".flac", ".ogg", ".mp4", ".mov", ".avi", ".webm", ".mkv",
    ".pptx", ".docx", ".xlsx", ".doc", ".xls", ".ppt",
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".bin", ".exe", ".o", ".a", ".class",
}
JUNK_BASENAMES = {".DS_Store", "Thumbs.db"}


def encode_binary(raw: bytes) -> str:
    """Encode raw bytes as a marked base64 string for extra_files storage."""
    return BINARY_MARKER + base64.b64encode(raw).decode("ascii")


def is_binary_value(value: object) -> bool:
    """True if an extra_files value is a marked base64 binary blob."""
    return isinstance(value, str) and value.startswith(BINARY_MARKER)


def decode_binary(value: str) -> bytes:
    """Decode a marked base64 string back to raw bytes."""
    return base64.b64decode(value[len(BINARY_MARKER):])


def encode_upload(filename: str, raw: bytes) -> str:
    """Encode one uploaded file for extra_files storage (text as-is, binary marked).

    Same text/binary decision as ``pack_directory`` / zip import: binary extension
    or undecodable bytes → marked base64, otherwise UTF-8 text. Shared by the
    admin / user single-file upload endpoints.
    """
    _, ext = os.path.splitext(filename)
    if ext.lower() in BINARY_EXTENSIONS:
        return encode_binary(raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return encode_binary(raw)


def pack_directory(root, *, skip_names: Iterable[str] = ()) -> Dict[str, str]:
    """Recursively pack a directory into an ``extra_files`` map.

    Binary files (by extension or undecodable bytes) → marked base64; text →
    UTF-8 as-is. Skips ``skip_names`` + junk basenames, enforces per-file and
    total size ceilings. Shared by marketplace install and plugin import.
    """
    root = Path(root)
    skip = set(skip_names) | JUNK_BASENAMES
    extra: Dict[str, str] = {}
    total = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name in skip:
            continue
        rel = p.relative_to(root).as_posix()
        if p.stat().st_size > MAX_SINGLE_FILE:
            continue
        raw = p.read_bytes()
        _, ext = os.path.splitext(rel)
        if ext.lower() in BINARY_EXTENSIONS:
            stored = encode_binary(raw)
        else:
            try:
                stored = raw.decode("utf-8")
            except UnicodeDecodeError:
                stored = encode_binary(raw)
        stored_size = len(stored.encode("utf-8"))
        if total + stored_size > MAX_TOTAL:
            continue
        total += stored_size
        extra[rel] = stored
    return extra
