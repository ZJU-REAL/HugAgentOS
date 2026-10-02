"""Bounded, content-addressed Office previews shared by local worker processes.

Callers MUST authorize the original file before entering this cache. The cache
is private to the OS user, never served as a static directory. Response files
are pinned with a hard link/copy so eviction cannot break an in-flight response.
"""

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time

from core.content.office import find_libreoffice_binary

CACHE_BYTES = 256 * 1024 * 1024
CACHE_ENTRIES = 128
CACHE_TTL = 24 * 3600
CONVERTERS = 2
LOCK_TIMEOUT = 150


def _try_lock(handle):
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return False


@contextmanager
def _lock_any(paths):
    handles = []
    acquired = None
    try:
        for path in paths:
            handle = open(path, "a+b")
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handles.append(handle)
        deadline = time.monotonic() + LOCK_TIMEOUT
        while acquired is None:
            for handle in handles:
                if _try_lock(handle):
                    acquired = handle
                    break
            if acquired is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Office 预览繁忙，请稍后重试")
                time.sleep(0.03)
        yield
    finally:
        if acquired is not None and os.name == "nt":
            import msvcrt

            acquired.seek(0)
            msvcrt.locking(acquired.fileno(), msvcrt.LK_UNLCK, 1)
        for handle in handles:
            handle.close()


def _version():
    binary = find_libreoffice_binary()
    if not binary:
        # Let the converter produce the normal actionable installation error.
        return "preview-v1:missing"
    path = Path(binary).resolve()
    stat = path.stat()
    return f"preview-v1:{path}:{stat.st_size}:{stat.st_mtime_ns}"


def _prune(root):
    now = time.time()
    for staging in root.glob("*.tmp"):
        staging.unlink(missing_ok=True)
    files = sorted(root.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    size = 0
    for i, path in enumerate(files):
        stat = path.stat()
        size += stat.st_size
        if i >= CACHE_ENTRIES or size > CACHE_BYTES or now - stat.st_mtime > CACHE_TTL:
            path.unlink(missing_ok=True)


def _pin(cached, destination):
    try:
        os.link(cached, destination)
    except OSError:
        shutil.copyfile(cached, destination)


def cached_office_pdf(source_path, file_id, convert, *, root=None, version=None):
    if root is None:
        uid = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "local")
        root = Path(tempfile.gettempdir()) / (
            "hugagent-preview-" + hashlib.sha256(uid.encode()).hexdigest()[:12]
        )
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="office-preview-response-"))
    try:
        # Hash an immutable request snapshot, not a path that an editor may
        # replace between hashing and conversion. Include the input extension.
        source = work / ("source" + Path(source_path).suffix.lower())
        shutil.copyfile(source_path, source)
        digest = hashlib.sha256()
        digest.update((version or _version()).encode())
        digest.update(b"\0")
        digest.update(source.suffix.encode())
        digest.update(b"\0")
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        key = digest.hexdigest()
        cached = root / (key + ".pdf")
        response = work / "preview.pdf"
        # Fixed stripes bound lock-file growth; OS locks release on crashes.
        with _lock_any([root / f"key-{int(key[:4],16)%64}.lock"]):
            with _lock_any([root / "maintenance.lock"]):
                if cached.exists() and time.time() - cached.stat().st_mtime <= CACHE_TTL:
                    _pin(cached, response)
                    os.utime(cached, None)
                    source.unlink()
                    return str(response), str(work)
            with _lock_any([root / f"converter-{i}.lock" for i in range(CONVERTERS)]):
                pdf, converted_work = convert(str(source), file_id)
                try:
                    with open(pdf, "rb") as stream:
                        if stream.read(5) != b"%PDF-":
                            raise RuntimeError("Office 预览未生成有效 PDF")
                    shutil.copyfile(pdf, response)
                finally:
                    shutil.rmtree(converted_work, ignore_errors=True)
            with _lock_any([root / "maintenance.lock"]):
                if response.stat().st_size <= CACHE_BYTES:
                    staging = root / (key + ".tmp")
                    try:
                        shutil.copyfile(response, staging)
                        os.replace(staging, cached)
                    finally:
                        staging.unlink(missing_ok=True)
                _prune(root)
        source.unlink()
        return str(response), str(work)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
