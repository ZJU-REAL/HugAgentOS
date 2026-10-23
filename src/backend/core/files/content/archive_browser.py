"""Read-only, bounded archive browsing. Member names never become disk paths."""

from __future__ import annotations

import bz2
import gzip
import lzma
import stat
import tarfile
import zipfile
import zlib

from core.files.content.archive_limits import check_zip_directory, open_bounded_xz
from collections import Counter
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 30 * 1024 * 1024
MAX_SCAN_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 5000
DECOMPRESSORS = {"gz": gzip.open, "bz2": bz2.open, "xz": open_bounded_xz}
TAR_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tbz",
    ".tbz2",
    ".txz",
    ".tar",
)
FORMAT_ERRORS = (
    zipfile.BadZipFile,
    tarfile.TarError,
    EOFError,
    OSError,
    zlib.error,
    lzma.LZMAError,
)


class ArchiveError(ValueError):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _safe_path(name: str) -> str | None:
    if not name or len(name) > 1024 or "\\" in name:
        return None
    if name.startswith("/") or any(ord(ch) < 32 for ch in name):
        return None
    while name.startswith("./"):
        name = name[2:]
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        return None
    return "/".join(parts)


def _entry(name: str, size: int, directory: bool, *, encrypted=False, special=False):
    path = _safe_path(name)
    if path is None:
        return None
    reason = (
        "archive_special_file"
        if special
        else (
            "archive_encrypted"
            if encrypted
            else "archive_member_too_large" if size > MAX_MEMBER_BYTES else None
        )
    )
    return {
        "path": path,
        "size": size,
        "is_dir": directory,
        "readable": not directory and reason is None,
        "reason": reason,
    }


def _validate_entries(entries):
    counts = Counter(entry["path"] for entry in entries)
    files = {entry["path"] for entry in entries if not entry["is_dir"]}
    for entry in entries:
        parents = PurePosixPath(entry["path"]).parents
        if counts[entry["path"]] > 1 or any(str(parent) in files for parent in parents):
            entry.update(readable=False, reason="archive_ambiguous_path")
    entries[:] = list({entry["path"]: entry for entry in entries}.values())
    return entries


def _zip_entries(archive):
    members = archive.infolist()
    if len(members) > MAX_ENTRIES:
        raise ArchiveError(413, "archive_too_many_entries")
    entries = []
    for info in members:
        mode = info.external_attr >> 16
        special = bool(stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
        entry = _entry(
            info.filename,
            info.file_size,
            info.is_dir(),
            encrypted=bool(info.flag_bits & 1),
            special=special,
        )
        if entry is not None:
            entries.append(entry)
    blocked = len(members) - len(entries)
    return _validate_entries(entries), blocked


def _check_zip(path):
    try:
        check_zip_directory(path, MAX_ENTRIES, 8 * 1024 * 1024)
    except ValueError as exc:
        code = {
            "entries": "archive_too_many_entries",
            "directory": "archive_directory_too_large",
            "unsupported": "archive_compression_unsupported",
        }.get(str(exc), "archive_invalid")
        status = (
            413
            if str(exc) in ("entries", "directory")
            else 415 if str(exc) == "unsupported" else 422
        )
        raise ArchiveError(status, code) from exc


def _check_source(path):
    if Path(path).stat().st_size > MAX_ARCHIVE_BYTES:
        raise ArchiveError(413, "archive_too_large")


def _bounded_read(stream, limit=MAX_MEMBER_BYTES):
    chunks = []
    size = 0
    while block := stream.read(min(64 * 1024, limit - size + 1)):
        size += len(block)
        if size > limit:
            raise ArchiveError(413, "archive_member_too_large")
        chunks.append(block)
    return b"".join(chunks)


def _selected(entries, path):
    if _safe_path(path) != path:
        raise ArchiveError(400, "archive_unsafe_path")
    entry = next((entry for entry in entries if entry["path"] == path), None)
    if entry is None or entry["is_dir"]:
        raise ArchiveError(404, "archive_member_missing")
    if not entry["readable"]:
        raise ArchiveError(
            413 if entry["reason"] == "archive_member_too_large" else 415, entry["reason"]
        )
    return entry


class _ScanBudget:
    """Count expanded bytes, including TAR headers and skipped file payloads."""

    def __init__(self, stream):
        self.stream = stream
        self.total = 0

    def read(self, size):
        data = self.stream.read(min(size, MAX_SCAN_BYTES - self.total + 1))
        self.total += len(data)
        if self.total > MAX_SCAN_BYTES:
            raise ArchiveError(413, "archive_scan_too_large")
        return data


def _format(name):
    name = name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(TAR_SUFFIXES):
        return "tar"
    suffix = name.rsplit(".", 1)[-1]
    if suffix in DECOMPRESSORS:
        return suffix
    raise ArchiveError(415, "archive_format_unsupported")


def _tar(path, name, member_path=None):
    entries, count, data = [], 0, None
    lower = name.lower()
    compression = (
        "gz"
        if lower.endswith((".gz", ".tgz"))
        else (
            "bz2"
            if lower.endswith((".bz2", ".tbz", ".tbz2"))
            else "xz" if lower.endswith((".xz", ".txz")) else None
        )
    )
    with ExitStack() as stack:
        stream = stack.enter_context(
            DECOMPRESSORS[compression](path, "rb") if compression else open(path, "rb")
        )
        archive = stack.enter_context(tarfile.open(fileobj=_ScanBudget(stream), mode="r|"))
        for info in archive:
            count += 1
            if count > MAX_ENTRIES:
                raise ArchiveError(413, "archive_too_many_entries")
            entry = _entry(
                info.name, info.size, info.isdir(), special=not (info.isfile() or info.isdir())
            )
            if entry is not None:
                entries.append(entry)
                if entry["path"] == member_path and entry["readable"]:
                    extracted = archive.extractfile(info)
                    if extracted is not None:
                        with extracted:
                            data = _bounded_read(extracted)
    blocked = count - len(entries)
    _validate_entries(entries)
    if member_path is not None:
        _selected(entries, member_path)
        if data is None:
            raise ArchiveError(422, "archive_invalid")
    return entries, blocked, data


def _single(path, name, kind, member_path=None):
    # Gzip header filenames are untrusted. Use the uploaded filename instead.
    entry_name = PurePosixPath(name).name.rsplit(".", 1)[0]
    with DECOMPRESSORS[kind](path, "rb") as stream:
        data = _bounded_read(stream)
    entry = _entry(entry_name, len(data), False)
    entries = [entry] if entry else []
    if member_path is not None:
        _selected(entries, member_path)
    return entries, 0 if entry else 1, data


def inspect_archive(path: str, name: str) -> dict:
    _check_source(path)
    kind = _format(name)
    try:
        if kind == "zip":
            _check_zip(path)
            with zipfile.ZipFile(path) as archive:
                entries, blocked = _zip_entries(archive)
        elif kind == "tar":
            entries, blocked, _ = _tar(path, name)
        else:
            entries, blocked, _ = _single(path, name, kind)
        return {"format": kind, "entries": entries, "blocked_count": blocked}
    except lzma.LZMAError as exc:
        if "Memory usage limit exceeded" in str(exc):
            raise ArchiveError(413, "archive_decoder_memory") from exc
        raise ArchiveError(422, "archive_invalid") from exc
    except FORMAT_ERRORS as exc:
        raise ArchiveError(422, "archive_invalid") from exc


def read_archive_member(path: str, name: str, member_path: str) -> bytes:
    _check_source(path)
    if _safe_path(member_path) != member_path:
        raise ArchiveError(400, "archive_unsafe_path")
    kind = _format(name)
    try:
        if kind == "tar":
            return _tar(path, name, member_path)[2]
        if kind != "zip":
            return _single(path, name, kind, member_path)[2]
        _check_zip(path)
        with zipfile.ZipFile(path) as archive:
            entries, _ = _zip_entries(archive)
            _selected(entries, member_path)
            info = next(
                info for info in archive.infolist() if _safe_path(info.filename) == member_path
            )
            with archive.open(info) as stream:
                return _bounded_read(stream)
    except lzma.LZMAError as exc:
        if "Memory usage limit exceeded" in str(exc):
            raise ArchiveError(413, "archive_decoder_memory") from exc
        raise ArchiveError(422, "archive_invalid") from exc
    except FORMAT_ERRORS as exc:
        raise ArchiveError(422, "archive_invalid") from exc
    except (RuntimeError, NotImplementedError) as exc:
        raise ArchiveError(415, "archive_compression_unsupported") from exc
