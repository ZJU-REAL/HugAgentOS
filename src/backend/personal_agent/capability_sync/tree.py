"""Source trees: the files of a skill or plugin directory, to hash and to copy into a workspace.

A tree is {relative path: (bytes, executable)}. Links, sockets and other special files are left out,
so a copy never carries a link that could point out of the workspace.
"""
import hashlib
import os
import stat

MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_TREE_FILES = 5000
# Build and editor leftovers that are never part of a skill.
SKIP_NAMES = frozenset({"__pycache__", ".DS_Store", ".git"})


class TreeTooLarge(ValueError):
    """A skill or plugin directory is larger than a workspace copy may be."""


def read_dir(path):
    """The tree of a directory on the platform's side (the repo or the skill loader's cache)."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: return _read(fd)
    finally: os.close(fd)


def read_at(parent_fd, name):
    """The tree of a directory inside a workspace directory, opened without following links."""
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try: return _read(fd)
    finally: os.close(fd)


def _read(root_fd):
    tree, size = {}, [0]

    def add(rel, data, executable):
        tree[rel] = (data, executable)
        size[0] += len(data)
        if len(tree) > MAX_TREE_FILES:
            raise TreeTooLarge("more than " + str(MAX_TREE_FILES) + " files")
        if size[0] > MAX_TREE_BYTES:
            raise TreeTooLarge("larger than " + str(MAX_TREE_BYTES // (1024 * 1024)) + " MiB")

    def walk(fd, prefix):
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if name in SKIP_NAMES: continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try: walk(child, prefix + name + "/")
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode):
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(handle, "rb") as stream:
                    add(prefix + name, stream.read(MAX_TREE_BYTES + 1), bool(info.st_mode & 0o111))

    walk(root_fd, "")
    return tree


def nest(prefix, tree):
    """The same files under a subdirectory, as a plugin holds its skills."""
    return {prefix + "/" + rel: item for rel, item in tree.items()}


def digest(tree):
    """sha256 over the sorted relative paths and their bytes: the hash the manifests record."""
    h = hashlib.sha256()
    for rel in sorted(tree):
        data = tree[rel][0]
        h.update(rel.encode("utf-8") + b"\0" + len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()
