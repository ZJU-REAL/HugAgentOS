#!/usr/bin/env python3
"""Create the single-file CE payload embedded in desktop installers."""

from __future__ import annotations

import argparse
import hashlib
import json
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
    "src/frontend/dist/workbench-build.json",
)


def validate_workbench_payload(source: Path) -> None:
    """Verify offline artifacts before the immutable server archive is created."""
    diagnostic = json.loads((source / "src/frontend/dist/workbench-build.json").read_text())
    if diagnostic.get("schemaVersion") != 1 or diagnostic.get("pluginUiVersions") != [1, 2, 3]:
        raise ValueError("workbench payload has incompatible build diagnostics")
    for name in ("workbench-protocol", "workbench-sdk"):
        package = source / "packages" / name
        metadata = json.loads((package / "package.json").read_text())
        record = diagnostic.get("packages", {}).get(name, {})
        version = diagnostic.get("protocolVersion" if name.endswith("protocol") else "sdkVersion")
        if metadata.get("version") != version or record.get("version") != version:
            raise ValueError(f"workbench package version mismatch: {name}")
        artifacts = record.get("artifacts", {})
        required = {"LICENSE", "package-lock.json", "dist/index.js", "dist/index.d.ts"}
        if name.endswith("protocol"):
            required.update(("dist/legacy.js", "dist/legacy.d.ts", "schemas/host.json"))
        if not required.issubset(artifacts):
            raise ValueError(f"workbench payload has missing artifact records: {name}")
        for relative, digest in artifacts.items():
            path = package / relative
            if not path.resolve().is_relative_to(package.resolve()) or path.is_symlink():
                raise ValueError(f"workbench artifact escapes package: {name}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"workbench artifact hash mismatch: {name}/{relative}")
        license_file = source / "src/frontend/dist/licenses" / f"{name}.txt"
        if hashlib.sha256(license_file.read_bytes()).hexdigest() != artifacts["LICENSE"]:
            raise ValueError(f"workbench distributed license mismatch: {name}")
    frontend_lock = source / "src/frontend/package-lock.json"
    if hashlib.sha256(frontend_lock.read_bytes()).hexdigest() != diagnostic.get(
        "frontendLockSha256"
    ):
        raise ValueError("workbench frontend lock hash mismatch")


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

    validate_workbench_payload(source)

    files: list[tuple[Path, Path]] = []
    for path in source.rglob("*"):
        if path.name == "node_modules":
            raise ValueError("CE payload must not contain build-time node_modules")
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
