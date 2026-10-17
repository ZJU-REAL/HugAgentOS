"""Files for Personal Agent."""
import hashlib

import os

import posixpath

import stat

from pathlib import Path

class WorkspaceUnavailable(RuntimeError):
    """The permanent workspace volume is not configured on this backend."""

def local_workspace(ident):
    root = os.getenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", "").strip() or os.getenv("PERSONAL_AGENT_VOLUME_ROOT", "").strip()
    if not root or not Path(root).is_absolute():
        raise WorkspaceUnavailable("Personal workspace volume is not configured")
    return Path(root) / ident / "workspace"

def relative(path):
    """A normalized workspace-relative path. Accepts "/workspace/x" or "x"."""
    from personal_agent.workspace.constants import WORKSPACE
    if not isinstance(path, str) or "\x00" in path: raise ValueError("Invalid path")
    path = path.strip()
    if path in ("", WORKSPACE, WORKSPACE + "/", "/", "."): return ""
    if path.startswith(WORKSPACE + "/"): path = path[len(WORKSPACE) + 1:]
    if path.startswith("/"): raise ValueError("Path must be inside /workspace")
    path = posixpath.normpath(path)
    if path == ".": return ""
    if path.startswith("..") or "/../" in "/" + path + "/": raise ValueError("Path must be inside /workspace")
    return path

def editable(rel):
    from personal_agent.workspace.constants import CONTEXT_FILES
    from personal_agent.workspace.constants import HEARTBEAT_FILE
    if rel in CONTEXT_FILES or rel == HEARTBEAT_FILE: return True
    parts = rel.split("/")
    return len(parts) == 2 and parts[0] == "memory" and parts[1].endswith(".md") and not parts[1].startswith(".")

def _open_dir(root, parts, create=False):
    """Walk directories without following links; returns an fd for the last one."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                try: os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError: pass
                else: _own(part, fd)
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise

def _own(name, dir_fd):
    # The task user owns the workspace. A non-root backend keeps its own ownership.
    from personal_agent.workspace.constants import TASK_UID
    try: os.chown(name, TASK_UID, TASK_UID, dir_fd=dir_fd, follow_symlinks=False)
    except (PermissionError, NotImplementedError, OSError): pass

def _read(root, rel, limit, *, tail=False):
    parts = rel.split("/")
    dfd = _open_dir(root, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    finally:
        os.close(dfd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode): raise IsADirectoryError(rel)
        if tail and info.st_size > limit: handle.seek(info.st_size - limit)
        return handle.read(limit + 1), info

def read_text(ident, rel):
    """The file's text, or None when it does not exist, is a link or is not a regular file."""
    from personal_agent.workspace.constants import READ_MAX_BYTES
    try:
        data, _ = _read(local_workspace(ident), rel, READ_MAX_BYTES, tail=rel.startswith("memory/") or rel == "DREAMS.md")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return None
    return data[:READ_MAX_BYTES].decode("utf-8", errors="replace")

def etag(data):
    return hashlib.sha256(data).hexdigest()[:32]

def read_file(ident, path):
    from personal_agent.workspace.constants import EDIT_MAX_BYTES
    from personal_agent.workspace.constants import READ_MAX_BYTES
    from personal_agent.workspace.constants import ROLES
    from personal_agent.workspace.constants import WORKSPACE
    rel = relative(path)
    if not rel: raise IsADirectoryError("Path is a directory")
    root = local_workspace(ident)
    try:
        data, info = _read(root, rel, READ_MAX_BYTES, tail=rel.startswith("memory/") or rel == "DREAMS.md")
    except FileNotFoundError:
        raise LookupError("File not found") from None
    except OSError as exc:
        # ELOOP: the final component is a link. The backend never follows one.
        raise PermissionError("File cannot be opened") from exc
    truncated = info.st_size > READ_MAX_BYTES
    data = data[:READ_MAX_BYTES]
    binary = b"\x00" in data[:8192]
    text = None
    if not binary:
        try: text = data.decode("utf-8")
        except UnicodeDecodeError:
            if truncated: text = data.decode("utf-8", errors="ignore")
            else: binary = True
    return {"path": WORKSPACE + "/" + rel, "size": info.st_size, "modified": info.st_mtime,
            "binary": binary, "truncated": truncated, "content": ("[Earlier daily records omitted; showing the latest records.]\n" + text if truncated and rel.startswith("memory/") and text is not None else text), "etag": etag(data),
            "editable": editable(rel) and not binary and info.st_size <= EDIT_MAX_BYTES, "role": ROLES.get(rel)}

def write_file(ident, path, content, expected=None):
    """Replace one persona file atomically. expected is the etag the editor started from."""
    from personal_agent.workspace.file_memory import workspace_lock
    with workspace_lock(ident):
        return _write_file(ident, path, content, expected)

def _write_file(ident, path, content, expected=None):
    from personal_agent.workspace.constants import EDIT_MAX_BYTES
    from personal_agent.workspace.constants import READ_MAX_BYTES
    rel = relative(path)
    if not editable(rel): raise PermissionError("Only workspace persona and memory files can be edited")
    data = content.encode("utf-8")
    if len(data) > EDIT_MAX_BYTES: raise ValueError("File exceeds 256 KiB")
    root = local_workspace(ident)
    if not root.exists(): raise LookupError("Workspace not created yet")
    parts = rel.split("/")
    dfd = _open_dir(root, parts[:-1], create=True)
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode): raise ValueError("Memory is not a regular file")
                if info.st_size > EDIT_MAX_BYTES: raise ValueError("Large memory files cannot be replaced; append new daily entries instead")
                current = etag(handle.read(READ_MAX_BYTES))
        except FileNotFoundError:
            current = None
        if expected is not None and expected != (current or ""):
            raise ValueError("File changed since it was opened")
        temporary = "." + parts[-1] + "." + os.urandom(6).hex() + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _own(temporary, dfd)
            os.replace(temporary, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
            os.fsync(dfd)
        except BaseException:
            try: os.unlink(temporary, dir_fd=dfd)
            except OSError: pass
            raise
    finally:
        os.close(dfd)
    return read_file(ident, rel)

def tree(ident):
    from personal_agent.workspace.constants import ROLES
    from personal_agent.workspace.constants import TREE_MAX_DEPTH
    from personal_agent.workspace.constants import TREE_MAX_ENTRIES
    from personal_agent.workspace.constants import WORKSPACE
    root = local_workspace(ident)
    if not root.exists():
        return {"root": WORKSPACE, "exists": False, "entries": [], "truncated": False}
    entries, truncated = [], False

    def walk(fd, prefix, depth):
        nonlocal truncated
        with os.scandir(fd) as items:
            children = sorted(items, key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.lower()))
        for item in children:
            if len(entries) >= TREE_MAX_ENTRIES:
                truncated = True
                return
            rel = prefix + item.name
            if item.name.startswith(".") and item.name.endswith(".tmp"): continue
            info = item.stat(follow_symlinks=False)
            kind = "link" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
            entries.append({"path": WORKSPACE + "/" + rel, "name": item.name, "type": kind,
                            "size": info.st_size if kind == "file" else None, "modified": info.st_mtime,
                            "role": ROLES.get(rel), "editable": kind == "file" and editable(rel)})
            if kind == "dir":
                if depth >= TREE_MAX_DEPTH:
                    truncated = True
                    continue
                try: child = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError: continue
                try: walk(child, rel + "/", depth + 1)
                finally: os.close(child)

    fd = _open_dir(root, [])
    try: walk(fd, "", 1)
    finally: os.close(fd)
    return {"root": WORKSPACE, "exists": True, "entries": entries, "truncated": truncated}

def _raw(ident, rel):
    from personal_agent.workspace.constants import READ_MAX_BYTES
    try: return _read(local_workspace(ident), rel, READ_MAX_BYTES)[0]
    except OSError: return None
