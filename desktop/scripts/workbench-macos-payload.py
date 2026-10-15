#!/usr/bin/env python3
"""Compare a native sealed app and its updater payload without extracting untrusted paths."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import tarfile


def digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(block)
    return result.hexdigest()


def compare_payload(app, archive_path, version, identifier):
    app = Path(app).resolve(strict=True)
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError("Expected a real .app directory")
    spec = importlib.util.spec_from_file_location("publisher", Path(__file__).with_name("publish-desktop.py"))
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    publisher.validate_macos_archive(Path(archive_path), version)
    with (app / "Contents/Info.plist").open("rb") as stream:
        metadata = plistlib.load(stream)
    if metadata.get("CFBundleIdentifier") != identifier or metadata.get("CFBundleShortVersionString") != version:
        raise ValueError("Actual app product/version mismatch")
    local = set()
    for folder, directories, files in os.walk(app, followlinks=False):
        for name in directories + files:
            path = Path(folder) / name
            if name.startswith("._") or name == "__MACOSX":
                continue
            local.add(path.relative_to(app).as_posix())
    seen = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if parts[0] != app.name:
                raise ValueError("Updater app name differs from native bundle")
            relative = PurePosixPath(*parts[1:]).as_posix()
            if relative == ".":
                if not member.isdir():
                    raise ValueError("Application root is not a directory")
                continue
            if relative in seen:
                raise ValueError("Duplicate updater archive member")
            seen.add(relative)
            path = app / relative
            # Do not read through links planted inside the package.
            if not path.parent.resolve(strict=True).is_relative_to(app):
                raise ValueError("Application path escapes sealed bundle")
            if member.issym():
                if not path.is_symlink() or os.readlink(path) != member.linkname:
                    raise ValueError("Updater symlink differs from native bundle")
                if not path.resolve(strict=True).is_relative_to(app):
                    raise ValueError("Application symlink escapes bundle")
            elif member.isdir():
                if path.is_symlink() or not path.is_dir():
                    raise ValueError("Updater directory differs from native bundle")
            elif member.isfile():
                if path.is_symlink() or not path.is_file() or path.stat().st_size != member.size:
                    raise ValueError("Updater file differs from native bundle")
                with path.open("rb") as source, archive.extractfile(member) as packed:
                    if digest(source) != digest(packed):
                        raise ValueError("Updater bytes differ from native bundle")
                if path.stat().st_mode & 0o777 != member.mode & 0o777:
                    raise ValueError("Updater file permissions differ")
            else:
                raise ValueError("Unsupported updater archive member type")
    if seen != local:
        raise ValueError("Updater archive does not contain the complete native bundle")
    return {"identifier": identifier, "version": version, "members": len(seen), "nativePayloadMatch": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--identifier", required=True)
    args = parser.parse_args()
    print(json.dumps(compare_payload(args.app, args.archive, args.version, args.identifier)))
