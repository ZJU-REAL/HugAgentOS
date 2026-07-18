"""KB upload file validation: extension-first + file header (magic bytes) as fallback.

Deliberately does not use the browser-reported ``UploadFile.content_type``:
WPS Office, old IE versions, and different operating systems report inconsistent
MIME types for .docx/.doc (commonly ``application/octet-stream``), which is the
root cause of "invalid file format" false positives in production.
"""

from __future__ import annotations

from os.path import splitext

from core.infra.exceptions import InvalidFileTypeError


# Canonical MIME derived from the extension, passed to downstream parse_and_chunk / kb_service
EXTENSION_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Magic-bytes prefix for each extension; None means plain-text format, skip header check
_MAGIC_SIGNATURES: dict[str, list[bytes] | None] = {
    ".pdf": [b"%PDF-"],
    # OOXML (docx/xlsx/pptx) is essentially a zip
    ".docx": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    ".xlsx": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    # Legacy OLE Compound Document
    ".doc": [b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"],
    ".xls": [b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".jpg": [b"\xFF\xD8\xFF"],
    ".jpeg": [b"\xFF\xD8\xFF"],
    # webp needs to verify "WEBP" at offset 8, handled specially
    ".webp": [b"RIFF"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".txt": None,
    ".md": None,
    ".csv": None,
    ".json": None,
}

ALLOWED_EXTENSIONS: list[str] = sorted(EXTENSION_TO_MIME.keys())


def validate_kb_file(filename: str, content: bytes) -> tuple[str, str]:
    """Validate an uploaded file by extension + file header.

    Returns ``(extension, canonical_mime)``. Raises ``InvalidFileTypeError`` on validation failure.
    """
    ext = splitext(filename or "")[1].lower()
    if ext not in EXTENSION_TO_MIME:
        raise InvalidFileTypeError(
            allowed_types=ALLOWED_EXTENSIONS,
            actual_type=ext or "<no extension>",
        )

    signatures = _MAGIC_SIGNATURES.get(ext)
    if signatures is not None and not _matches_signature(content, ext, signatures):
        raise InvalidFileTypeError(
            allowed_types=ALLOWED_EXTENSIONS,
            actual_type=ext,
        )

    return ext, EXTENSION_TO_MIME[ext]


def _matches_signature(content: bytes, ext: str, signatures: list[bytes]) -> bool:
    if not content:
        return False
    if ext == ".webp":
        return (
            content[:4] == b"RIFF"
            and len(content) >= 12
            and content[8:12] == b"WEBP"
        )
    return any(content.startswith(sig) for sig in signatures)
