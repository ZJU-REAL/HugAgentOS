"""Pure workspace identities shared by host tools and the execution service.

Only /workspace and /myspace are logical aliases. Absolute host paths are
already identities and must never be expanded into a second session path.
No I/O, application startup, settings imports or permission decisions live here.
"""
import hashlib
import ntpath
import posixpath

def session_root(root: str, session_id: str) -> str:
    value = (session_id or "").strip()
    if not value or len(value) > 512:
        raise ValueError("invalid workspace session_id")
    pathmod = ntpath if ntpath.splitdrive(root)[0] else posixpath
    return pathmod.join(root, ".sessions", hashlib.sha256(value.encode("utf-8")).hexdigest())

def resolve_path(path: str, root: str, session_id: str, user_id=None) -> str:
    pathmod = ntpath if ntpath.splitdrive(root)[0] else posixpath
    if path == "/myspace" or path.startswith("/myspace/"):
        if not user_id:
            raise ValueError("MySpace requires a user identity")
        path = "/workspace/myspace/" + user_id + path[len("/myspace"):]
    if path == "/workspace" or path.startswith("/workspace/"):
        parts = [part for part in path[len("/workspace"):].split("/") if part]
        return pathmod.join(session_root(root, session_id), *parts)
    return path
