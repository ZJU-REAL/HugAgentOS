"""Read management inputs into a validated snapshot; never execute package code."""
from contextlib import contextmanager
from pathlib import Path
import io
import tarfile
import tempfile
import zipfile
from core.capabilities import archive
from core.capabilities.errors import IntegrityFailed


def _tar_files(data):
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as package:
        for entry in package:
            name = entry.name
            while name.startswith("./"):
                name = name[2:]
            if entry.isdir():
                continue
            if not entry.isfile() or name in files:
                raise IntegrityFailed("links, special and duplicate entries are forbidden")
            total += entry.size
            if (len(files) >= archive.MAX_MEMBERS or entry.size > archive.MAX_MEMBER_BYTES
                    or total > archive.MAX_TOTAL_BYTES):
                raise IntegrityFailed("package exceeds size limits")
            archive._normalize_member(name)
            with package.extractfile(entry) as stream:
                files[name] = stream.read(entry.size + 1)
            if len(files[name]) != entry.size:
                raise IntegrityFailed("invalid archive size")
    return files


@contextmanager
def snapshot(source_path):
    source = Path(source_path).expanduser()
    if not source.is_absolute():
        raise ValueError("source_path must be an absolute path")
    with tempfile.TemporaryDirectory(prefix="manager-package-") as temp:
        root = Path(temp) / "package"
        if source.is_dir():
            files, total = {}, 0
            for name, file, stat in archive.iter_file_stats(source):
                if stat.st_size > archive.MAX_MEMBER_BYTES:
                    raise IntegrityFailed("package member too large")
                total += stat.st_size
                if total > archive.MAX_TOTAL_BYTES or len(files) >= archive.MAX_MEMBERS:
                    raise IntegrityFailed("package exceeds size limits")
                with file.open("rb") as stream:
                    raw = stream.read(min(stat.st_size + 1, archive.MAX_MEMBER_BYTES + 1))
                if len(raw) != stat.st_size:
                    raise IntegrityFailed("source changed while reading")
                files[name] = raw
            archive.write_files(root, files)
        else:
            with source.open("rb") as stream:
                data = stream.read(archive.MAX_TOTAL_BYTES + 1)
            if len(data) > archive.MAX_TOTAL_BYTES:
                raise IntegrityFailed("archive too large")
            if zipfile.is_zipfile(io.BytesIO(data)):
                archive.extract_zip(data, root)
            else:
                archive.write_files(root, _tar_files(data))
        # Permit one conventional wrapper folder; never select the first of many roots.
        if not any((root / name).is_file() for name in ("SKILL.md", "plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json")):
            children = list(root.iterdir())
            if len(children) == 1 and children[0].is_dir():
                root = children[0]
        yield root


def serialized(function):
    """Serialize device management mutations across processes, with a bounded wait."""
    from functools import wraps
    @wraps(function)
    def run(*args, **kwargs):
        from core.capabilities.lockfile import locked
        from core.capabilities.paths import meta_root
        with locked(meta_root() / "management.lock", timeout=35):
            return function(*args, **kwargs)
    return run
