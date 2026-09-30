"""File operations confined to a workspace, even during sandbox symlink changes."""

import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

MAX_FILE_BYTES = 50 * 1024 * 1024


def _parts(name):
    parts = name.split("/")
    if (
        not parts
        or "\\" in name
        or any(p in ("", ".", "..") or any(ord(c) < 32 for c in p) for p in parts)
    ):
        raise HTTPException(400, "非法项目文件路径")
    return parts


def source_path(root: Path, name: str) -> Path:
    parts = _parts(name)
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or any(
        root.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1)
    ):
        raise HTTPException(400, "项目文件不能通过符号链接访问")
    return path


def _own(fd):
    from core.sandbox._common import SANDBOX_RUN_GID, SANDBOX_RUN_UID

    if os.geteuid() == 0:
        os.fchown(fd, SANDBOX_RUN_UID, SANDBOX_RUN_GID)


@contextmanager
def parent_fd(root: Path, name: str, *, create=False):
    parts = _parts(name)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if create:
                _own(descriptor)
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def read_source(root: Path, name: str, *, max_bytes=MAX_FILE_BYTES) -> bytes:
    with parent_fd(root, name) as (parent, leaf):
        descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise HTTPException(400, "项目源码必须是普通文件")
            data = file.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise HTTPException(413, f"项目单文件超过限制：{name}")
            return data


def write_source(root: Path, name: str, data: bytes) -> None:
    with parent_fd(root, name, create=True) as (parent, leaf):
        temporary = ".workspace-" + uuid.uuid4().hex
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent
        )
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                _own(file.fileno())
            os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def delete_source(root: Path, name: str) -> None:
    with parent_fd(root, name) as (parent, leaf):
        os.unlink(leaf, dir_fd=parent)
