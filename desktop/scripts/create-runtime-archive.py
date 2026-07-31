#!/usr/bin/env python3
"""Create a deterministic tar.gz archive for a relocatable desktop runtime."""

from __future__ import annotations

import argparse
import gzip
import os
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


REQUIRED_FILES = ("runtime-layout.json", "runtime-smoke.py")


def _long_path(path: Path) -> Path:
    """Use Win32 extended-length paths without requiring a machine policy change."""
    if os.name != "nt":
        return path
    raw = str(path)
    if raw.startswith("\\\\?\\"):
        return path
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def _safe_link(path: Path, source: Path) -> None:
    target = Path(os.readlink(path))
    if target.is_absolute():
        raise ValueError(f"Runtime contains an absolute symlink: {path} -> {target}")
    relative = path.relative_to(source)
    resolved_parts: list[str] = []
    for part in PurePosixPath(relative.parent.as_posix(), target.as_posix()).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not resolved_parts:
                raise ValueError(f"Runtime symlink escapes its root: {path} -> {target}")
            resolved_parts.pop()
        else:
            resolved_parts.append(part)


def _normalized_info(path: Path, source: Path) -> tarfile.TarInfo:
    relative = path.relative_to(source).as_posix()
    info = tarfile.TarInfo(relative)
    stat = path.lstat()
    info.mode = stat.st_mode & 0o7777
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if path.is_symlink():
        _safe_link(path, source)
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
    elif path.is_dir():
        info.type = tarfile.DIRTYPE
    elif path.is_file():
        info.type = tarfile.REGTYPE
        info.size = stat.st_size
    else:
        raise ValueError(f"Unsupported runtime entry: {path}")
    return info


def create_archive(source: Path, output: Path) -> tuple[int, int]:
    # Some SDK wheels contain relative paths long enough to exceed MAX_PATH
    # once placed below a release checkout. Prefix every filesystem operation
    # on Windows so archive creation works even when LongPathsEnabled is off.
    source = _long_path(source.resolve(strict=True))
    output = _long_path(output.resolve())
    if not source.is_dir():
        raise ValueError(f"Runtime source isn't a directory: {source}")
    for relative in REQUIRED_FILES:
        if not (source / relative).is_file():
            raise ValueError(f"Runtime is missing required file: {relative}")
    if output == source or source in output.parents:
        raise ValueError("Runtime archive output must be outside the runtime directory")

    entries = sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix())
    unpacked_size = sum(path.stat().st_size for path in entries if path.is_file())
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for path in entries:
                        info = _normalized_info(path, source)
                        if info.isreg():
                            with path.open("rb") as input_file:
                                archive.addfile(info, input_file)
                        else:
                            archive.addfile(info)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(entries), unpacked_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    entries, size = create_archive(args.source, args.output)
    print(f"Archived {entries} runtime entries ({size} bytes) into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
