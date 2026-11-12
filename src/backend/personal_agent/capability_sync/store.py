"""The workspace side of capability sync: /workspace/skills and /workspace/plugins, their manifests,
and replacing one entry at a time.

Everything is opened relative to a directory fd with O_NOFOLLOW, as personal_agent.workspace.files
does: the agent owns its workspace and may have put links anywhere in it, and the backend never
follows one. An entry is written into a hidden temporary directory next to it and renamed into place,
so the agent sees either the old copy or the new one, never a half-written directory.
"""
import errno
import fcntl
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager

from personal_agent.workspace import files

MANIFEST = ".synced.json"
MANIFEST_VERSION = 1
SKILLS, PLUGINS = "skills", "plugins"
# One path component the sync may create or delete. Ids from the catalog, the plugin slugs and a
# manifest the agent may have edited all pass through it, so a name can never leave the directory.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
_DIR_MODE, _FILE_MODE, _EXEC_MODE = 0o755, 0o644, 0o755


def valid_name(name):
    return isinstance(name, str) and bool(_NAME.fullmatch(name)) and ".." not in name


def _workspace(agent_id):
    """The workspace root, created the way persona.seed creates it when it is not there yet."""
    root = files.local_workspace(agent_id)
    root.mkdir(parents=True, exist_ok=True)
    try: os.chmod(root, 0o700)
    except OSError: pass
    return root


@contextmanager
def agent_lock(agent_id):
    """One sync at a time per agent, across backend processes. The lock file sits next to the
    workspace, outside what the agent can reach, and is separate from the memory lock so a long copy
    never makes a memory write wait."""
    root = _workspace(agent_id)
    fd = os.open(root.parent / ".capability-sync.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def opened(agent_id, kind):
    """An fd on /workspace/<kind>, created when missing."""
    fd = files._open_dir(_workspace(agent_id), [kind], create=True)
    try: yield fd
    finally: os.close(fd)


def kind_of(fd, name):
    """'dir', 'other' (a file or a link the agent put there) or None when nothing has that name."""
    try: info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError: return None
    return "dir" if stat.S_ISDIR(info.st_mode) else "other"


def names(fd):
    """The visible entries of the directory: what may belong to the agent itself."""
    with os.scandir(fd) as entries:
        return {entry.name for entry in entries if not entry.name.startswith(".")}


def read_manifest(fd, kind):
    """The entries the sync wrote last time, {} when there is no usable manifest."""
    try:
        handle = os.open(MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except OSError:
        return {}
    try:
        with os.fdopen(handle, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): return {}
            data = json.loads(stream.read(1024 * 1024).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    entries = data.get(kind) if isinstance(data, dict) and data.get("version") == MANIFEST_VERSION else None
    if not isinstance(entries, dict): return {}
    return {name: entry for name, entry in entries.items() if valid_name(name) and isinstance(entry, dict)}


def write_manifest(fd, kind, entries):
    body = json.dumps({"version": MANIFEST_VERSION, kind: dict(sorted(entries.items()))},
                      ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    temporary = "." + MANIFEST + "." + os.urandom(6).hex() + ".tmp"
    _write_file(fd, temporary, body, _FILE_MODE)
    try:
        os.replace(temporary, MANIFEST, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        _discard(fd, temporary)
        raise


def replace(fd, name, tree):
    """Put tree at name: a staged copy renamed into place, then the previous one removed."""
    staged = "." + name + "." + os.urandom(6).hex() + ".tmp"
    os.mkdir(staged, _DIR_MODE, dir_fd=fd)
    files._own(staged, fd)
    try:
        root = os.open(staged, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            for rel in sorted(tree):
                data, executable = tree[rel]
                parts = rel.split("/")
                parent = _subdir(root, parts[:-1])
                try: _write_file(parent, parts[-1], data, _EXEC_MODE if executable else _FILE_MODE)
                finally:
                    if parent != root: os.close(parent)
        finally:
            os.close(root)
        previous = None
        if kind_of(fd, name) is not None:
            previous = "." + name + "." + os.urandom(6).hex() + ".old.tmp"
            os.rename(name, previous, src_dir_fd=fd, dst_dir_fd=fd)
        os.rename(staged, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        _discard(fd, staged)
        raise
    if previous: _discard(fd, previous)


def remove(fd, name):
    _discard(fd, name)


def sweep_staged(fd):
    """Staged copies a crashed sync left behind."""
    with os.scandir(fd) as entries:
        leftovers = [entry.name for entry in entries if entry.name.startswith(".") and entry.name.endswith(".tmp")]
    for name in leftovers: _discard(fd, name)


def _subdir(root, parts):
    fd = root
    for part in parts:
        # Platform trees come from the repo or the skill loader, but a name is still never a path.
        if part in ("", ".", ".."): raise ValueError("Invalid path in skill files")
        try:
            os.mkdir(part, _DIR_MODE, dir_fd=fd)
            files._own(part, fd)
        except FileExistsError:
            pass
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        if fd != root: os.close(fd)
        fd = child
    return fd


def _write_file(fd, name, data, mode):
    if name in ("", ".", "..") or "/" in name: raise ValueError("Invalid path in skill files")
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
    with os.fdopen(handle, "wb") as stream:
        # O_CREAT applies the umask; the copy keeps the source's executable bit either way.
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    files._own(name, fd)


def _discard(fd, name):
    """Remove an entry without following links: a directory tree fd by fd, anything else unlinked."""
    kind = kind_of(fd, name)
    if kind is None: return
    if kind == "dir":
        shutil.rmtree(name, dir_fd=fd)
        return
    try: os.unlink(name, dir_fd=fd)
    except OSError as exc:
        if exc.errno != errno.ENOENT: raise
