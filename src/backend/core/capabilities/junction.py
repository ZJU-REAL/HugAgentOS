"""Directory links that work on every platform we ship to — and never silently copy.

Windows uses native NTFS directory junctions (``mklink /J`` semantics),
which need neither administrator rights nor Developer Mode. POSIX uses symlinks,
relative when the target lives under the same tree so a bind-mounted copy still
resolves. A link that cannot be created raises; the caller reports
``view_unavailable`` instead of degrading to a directory copy that would go
stale and eat disk.
"""

from __future__ import annotations

import os
import ntpath
import stat
from functools import wraps
from pathlib import Path
from typing import Iterable, Optional

_IS_WINDOWS = os.name == "nt"
_WIN_PREFIX = "\\\\?\\"


class LinkError(RuntimeError):
    """A directory link could not be created, read or removed."""


class LinkTargetOutsideRoot(LinkError):
    """Refused: the target does not live under any allowed root."""


def _link_errors(action: str):
    """Keep platform IO failures inside the link API's documented error type."""

    def decorate(operation):
        @wraps(operation)
        def wrapped(*args, **kwargs):
            try:
                return operation(*args, **kwargs)
            except OSError as exc:
                path = args[0] if args else kwargs.get("path", kwargs.get("link", ""))
                raise LinkError(f"cannot {action} directory link {path}: {exc}") from exc

        return wrapped

    return decorate


def _strip_windows_prefix(raw: str) -> str:
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[8:]
    if raw.startswith(_WIN_PREFIX):
        return raw[len(_WIN_PREFIX) :]
    return raw


def _extended_windows_path(raw: str) -> str:
    """Extended Win32 paths work without changing machine-wide path policy."""
    raw = ntpath.abspath(raw)
    if raw.startswith(_WIN_PREFIX):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return _WIN_PREFIX + raw


def _native(path: Path) -> Path:
    return Path(_extended_windows_path(str(path))) if _IS_WINDOWS else path


def _resolved(path: Path) -> Path:
    raw = os.path.realpath(_native(path))
    return Path(_strip_windows_prefix(raw) if _IS_WINDOWS else raw)


def _lstat(path: Path) -> Optional[os.stat_result]:
    try:
        return os.lstat(_native(path))
    except FileNotFoundError:
        return None


@_link_errors("inspect")
def is_directory_link(path: Path) -> bool:
    st = _lstat(path)
    if st is None:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if _IS_WINDOWS:
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


@_link_errors("read")
def read_directory_link(path: Path) -> Optional[Path]:
    """Absolute target of a link, or None when ``path`` is not a link."""
    if not is_directory_link(path):
        return None
    raw = os.readlink(_native(path))
    if _IS_WINDOWS:
        raw = _strip_windows_prefix(raw)
    target = Path(raw)
    if not target.is_absolute():
        target = path.parent / target
    return Path(os.path.normpath(target))


def _within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.normcase(_resolved(path))).relative_to(os.path.normcase(_resolved(root)))
        return True
    except ValueError:
        return False


@_link_errors("validate")
def assert_target_allowed(target: Path, allowed_roots: Iterable[Path]) -> None:
    roots = list(allowed_roots)
    if not any(_within(target, root) for root in roots):
        raise LinkTargetOutsideRoot(
            f"link target {target} is outside the allowed roots {[str(r) for r in roots]}"
        )


@_link_errors("remove")
def remove_directory_link(path: Path) -> None:
    """Remove the link itself; never follows it, never deletes a real directory."""
    if not is_directory_link(path):
        if _native(path).exists():
            raise LinkError(f"{path} is a real directory, not a link; refusing to remove")
        return
    if _IS_WINDOWS:
        # Junctions and directory symlinks are removed with rmdir on NTFS.
        os.rmdir(_native(path))
    else:
        os.unlink(path)


def _create_windows_junction(link: Path, target: Path) -> None:
    """Create a mount-point reparse record with long-path-aware Win32 calls.

    CPython's private CreateJunction API prefixes the target internally and
    still rejects long unprefixed targets on some Windows installations. Using
    FSCTL_SET_REPARSE_POINT directly avoids both that limit and token-privilege
    manipulation; directory junctions do not require symlink privileges.
    """
    import ctypes
    import struct
    from ctypes import wintypes

    raw = str(target)
    substitute = "\\??\\UNC\\" + raw[2:] if raw.startswith("\\\\") else "\\??\\" + raw
    sub_bytes, print_bytes = substitute.encode("utf-16-le"), raw.encode("utf-16-le")
    names = sub_bytes + b"\0\0" + print_bytes + b"\0\0"
    data = struct.pack("<HHHH", 0, len(sub_bytes), len(sub_bytes) + 2, len(print_bytes)) + names
    record = struct.pack("<LHH", 0xA0000003, len(data), 0) + data
    if len(record) > 16 * 1024:
        raise OSError("junction target exceeds the Windows reparse-record limit")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    native_link = _native(link)
    native_link.mkdir()
    handle = kernel.CreateFileW(str(native_link), 0x40000000, 7, None, 3, 0x02200000, None)
    invalid = ctypes.c_void_p(-1).value
    try:
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(record)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(
            handle, 0x000900A4, buffer, len(record), None, 0, ctypes.byref(returned), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        if handle != invalid:
            kernel.CloseHandle(handle)
            handle = invalid
        # Only our new empty directory is removable. A populated directory is
        # preserved if something else changed it while creation was in flight.
        try:
            native_link.rmdir()
        except OSError:
            pass
        raise
    finally:
        if handle != invalid:
            kernel.CloseHandle(handle)


@_link_errors("create")
def create_directory_link(link: Path, target: Path, *, allowed_roots: Iterable[Path]) -> None:
    """Create ``link`` → ``target``. ``target`` must exist and be under an allowed root."""
    if not _native(target).is_dir():
        raise LinkError(f"link target does not exist or is not a directory: {target}")
    assert_target_allowed(target, allowed_roots)
    if _lstat(link) is not None:
        raise LinkError(f"link path already exists: {link}")
    _native(link.parent).mkdir(parents=True, exist_ok=True)
    if _IS_WINDOWS:
        try:
            _create_windows_junction(link, _resolved(target))
        except OSError as exc:
            raise LinkError(f"cannot create junction {link} -> {target}: {exc}") from exc
        return
    rel = os.path.relpath(target.resolve(), link.parent.resolve())
    try:
        os.symlink(rel, link, target_is_directory=True)
    except OSError as exc:
        raise LinkError(f"cannot create symlink {link} -> {target}: {exc}") from exc


@_link_errors("update")
def ensure_directory_link(link: Path, target: Path, *, allowed_roots: Iterable[Path]) -> bool:
    """Make ``link`` point at ``target``; returns True when something changed.

    A real directory sitting at ``link`` is user data and is never touched — the
    caller gets ``LinkError`` and reports the path.
    """
    allowed_roots = tuple(allowed_roots)
    wanted = Path(os.path.normpath(_resolved(target)))
    assert_target_allowed(wanted, allowed_roots)
    if not _native(wanted).is_dir():
        raise LinkError(f"link target does not exist or is not a directory: {wanted}")
    current = read_directory_link(link)
    if current is not None:
        if Path(os.path.normcase(_resolved(current))) == Path(os.path.normcase(_resolved(wanted))):
            return False
        remove_directory_link(link)
    elif _lstat(link) is not None:
        raise LinkError(f"{link} exists and is not a link; refusing to replace user data")
    create_directory_link(link, wanted, allowed_roots=allowed_roots)
    return True
