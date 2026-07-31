#!/usr/bin/env python3
"""Create the single-file CE payload embedded in desktop installers."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


REQUIRED_FILES = (
    "desktop-bundle.json",
    "pyproject.toml",
    "requirements-mem0.txt",
    "src/frontend/dist/index.html",
)


def _archive_file(archive: ZipFile, source: Path, relative: Path) -> None:
    info = ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (source.stat().st_mode & 0xFFFF) << 16
    with source.open("rb") as input_file, archive.open(info, "w") as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)


def create_archive(source: Path, output: Path) -> int:
    source = source.resolve(strict=True)
    output = output.resolve()
    if not source.is_dir():
        raise ValueError(f"CE payload source isn't a directory: {source}")
    for relative in REQUIRED_FILES:
        if not (source / relative).is_file():
            raise ValueError(f"CE payload is missing required file: {relative}")
    if output == source or source in output.parents:
        raise ValueError("CE archive output must be outside the payload directory")

    files: list[tuple[Path, Path]] = []
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"CE payload must not contain symlinks: {path}")
        if path.is_file():
            files.append((path, path.relative_to(source)))
    files.sort(key=lambda item: item[1].as_posix())

    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with ZipFile(
            temporary,
            mode="w",
            compression=ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path, relative in files:
                _archive_file(archive, path, relative)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(files)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = create_archive(args.source, args.output)
    print(f"Archived {count} CE payload files into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
