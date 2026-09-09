"""Portable directory archive program executed by every sandbox provider."""
import fnmatch
import json
import os
from pathlib import Path
import stat
import sys
import tarfile

def native(path):
    raw = os.path.abspath(path)
    slash = chr(92)
    prefix = slash * 2 + "?" + slash
    if os.name != "nt" or raw.startswith(prefix):
        return raw
    if raw.startswith(slash * 2):
        return prefix + "UNC" + slash + raw[2:]
    return prefix + raw

def raise_walk_error(error):
    raise error

def pack_directory(source, target, excludes, max_files, max_file_bytes, max_total_bytes, max_archive_bytes):
    root = Path(native(source))
    target = Path(native(target))
    if not root.is_dir():
        raise ValueError("site directory does not exist")
    def excluded(name):
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in excludes)

    total = count = 0
    try:
        with tarfile.open(target, "w:gz") as archive:
            for directory, dirs, names in os.walk(root, followlinks=False, onerror=raise_walk_error):
                base = Path(directory)
                dirs[:] = sorted(name for name in dirs if not excluded(name))
                for name in dirs + names:
                    entry = base / name
                    metadata = entry.lstat()
                    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                        raise ValueError("site contains linked content: " + entry.relative_to(root).as_posix())
                for name in sorted(names):
                    if excluded(name):
                        continue
                    entry = base / name
                    if entry == target:
                        continue
                    metadata = entry.stat()
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError("site contains a special file")
                    total += metadata.st_size
                    count += 1
                    if count > max_files or metadata.st_size > max_file_bytes or total > max_total_bytes:
                        raise ValueError("site exceeds file count or size limits")
                    archive.add(entry, arcname=entry.relative_to(root).as_posix(), recursive=False)
                    if target.stat().st_size > max_archive_bytes:
                        raise ValueError("site archive exceeds size limit")
        if target.stat().st_size > max_archive_bytes:
            raise ValueError("site archive exceeds size limit")
    except BaseException:
        target.unlink(missing_ok=True)
        raise

if __name__ == "__main__":
    options = json.load(sys.stdin)
    pack_directory(target="/workspace/" + options.pop("archive_name"), **options)
