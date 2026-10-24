"""Descriptor-relative storage operations on POSIX hosts."""
from contextlib import contextmanager
import os
import stat
import uuid
from pathlib import Path

from core.files.storage.entries import validate_key


@contextmanager
def directory_fd(root: Path, key="", *, create=False):
    parts = validate_key(key, directory=True).rstrip("/").split("/") if key else []
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def parent_fd(root, key, *, create=False):
    key = validate_key(key)
    parent, _, name = key.rpartition("/")
    with directory_fd(root, parent, create=create) as descriptor:
        yield descriptor, name


def read_bytes(root, key):
    with parent_fd(root, key) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Only regular files can be read")
            return stream.read()


def write_stream(root, key, source):
    with parent_fd(root, key, create=True) as (parent, name):
        temporary = ".upload-" + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                import shutil
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def file_stat(root, key):
    with parent_fd(root, key) as (parent, name):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Symbolic links are not storage objects")
        return info


def remove_file(root, key):
    with parent_fd(root, key) as (parent, name):
        os.unlink(name, dir_fd=parent)
