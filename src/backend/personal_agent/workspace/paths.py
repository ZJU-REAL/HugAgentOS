"""Paths for Personal Agent."""

import posixpath

def workspace_path(path):
    path = posixpath.normpath(path)
    if not path.startswith("/workspace/") or "\x00" in path:
        raise ValueError("Path must be inside /workspace")
    return path
