"""Portable package file validation shared by zip and in-memory installations."""

from __future__ import annotations
import io
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Tuple
from .errors import IntegrityFailed
from .paths import _WINDOWS_RESERVED

MAX_MEMBERS = 5000
MAX_DEPTH = 24
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveEntry:
    relative_path: str
    size: int


def _reject(name: str, why: str) -> None:
    raise IntegrityFailed(f"unsafe archive member {name!r}: {why}")


def _normalize_member(name: str) -> str:
    if not name:
        _reject(name, "empty name")
    if name.startswith("/") or "\\" in name:
        _reject(name, "absolute path or backslash separator")
    parts = name.split("/")
    if len(parts) > MAX_DEPTH:
        _reject(name, "too deep")
    for part in parts:
        if not part or part in (".", ".."):
            _reject(name, "empty or relative path component")
        if part.endswith((".", " ")) or any(ord(c) < 32 or c in '<>:"|?*' for c in part):
            _reject(name, "nonportable file name")
        if part.split(".")[0].upper() in _WINDOWS_RESERVED:
            _reject(name, "reserved file name")
        if len(part.encode("utf-16-le")) > 510:
            _reject(name, "file name too long")
    return "/".join(parts)


def _validate_names(names: List[str]) -> None:
    spellings: Dict[str, str] = {}
    files = set()
    directories = set()
    for name in names:
        parts = name.split("/")
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            folded = unicodedata.normalize("NFC", prefix).casefold()
            if folded in spellings and spellings[folded] != prefix:
                _reject(name, f"case-insensitive collision with {spellings[folded]!r}")
            spellings[folded] = prefix
            if count < len(parts):
                if folded in files:
                    _reject(name, "file is also a parent directory")
                directories.add(folded)
            else:
                if folded in files or folded in directories:
                    _reject(name, "duplicate file or directory collision")
                files.add(folded)


def inspect_zip(data: bytes) -> Tuple[zipfile.ZipFile, List[ArchiveEntry], str]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IntegrityFailed("not a zip archive") from exc
    try:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise IntegrityFailed("archive has too many members")
        normalized = []
        total = 0
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                _reject(info.filename, "links and special files are not package files")
            if info.create_system == 0 and info.external_attr & 0x400:
                _reject(info.filename, "reparse point")
            if info.flag_bits & 1:
                _reject(info.filename, "encrypted member")
            name = info.filename.rstrip("/") if info.is_dir() else info.filename
            norm = _normalize_member(name)
            if info.is_dir():
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                _reject(info.filename, "member too large")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise IntegrityFailed("archive expands beyond the allowed size")
            normalized.append((norm, info.file_size))
        if not normalized:
            raise IntegrityFailed("archive is empty")
        roots = {PurePosixPath(n).parts[0] for n, _ in normalized}
        strip = (
            next(iter(roots)) if len(roots) == 1 and all("/" in n for n, _ in normalized) else ""
        )
        entries = [
            ArchiveEntry(n[len(strip) + 1 :] if strip else n, size) for n, size in normalized
        ]
        _validate_names([entry.relative_path for entry in entries])
        return zf, entries, strip
    except Exception:
        zf.close()
        raise


def extract_zip(data: bytes, dest: Path) -> List[str]:
    zf, entries, strip = inspect_zip(data)
    dest.mkdir(parents=True, exist_ok=False)
    written = []
    with zf:
        for entry in entries:
            member = f"{strip}/{entry.relative_path}" if strip else entry.relative_path
            target = dest.joinpath(*PurePosixPath(entry.relative_path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                remaining = entry.size
                while remaining:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise IntegrityFailed("archive member was truncated")
                    out.write(chunk)
                    remaining -= len(chunk)
                if src.read(1):
                    raise IntegrityFailed("archive member exceeded declared size")
            written.append(entry.relative_path)
    return written


def write_files(dest: Path, files: Dict[str, bytes | str]) -> List[str]:
    if not files or len(files) > MAX_MEMBERS:
        raise IntegrityFailed("invalid package member count")
    prepared = []
    total = 0
    for rel, body in files.items():
        norm = _normalize_member(rel)
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        total += len(data)
        if len(data) > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
            raise IntegrityFailed("package exceeds the allowed size")
        prepared.append((norm, data))
    _validate_names([name for name, _ in prepared])
    dest.mkdir(parents=True, exist_ok=False)
    for name, data in prepared:
        target = dest.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return [name for name, _ in prepared]


def iter_files(root: Path) -> Iterable[Tuple[str, Path]]:
    from .junction import is_directory_link

    if is_directory_link(root):
        raise IntegrityFailed("package root must be a real directory")
    for path in sorted(root.rglob("*")):
        if is_directory_link(path):
            raise IntegrityFailed(f"package contains linked content: {path.name}")
        if path.is_file():
            yield path.relative_to(root).as_posix(), path
