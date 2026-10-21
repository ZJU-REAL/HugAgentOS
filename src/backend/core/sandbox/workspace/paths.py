"""Pure session path naming and the configured sandbox workspace root."""

from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath


def session_root(root: str, session_id: str) -> str:
    value = (session_id or "").strip()
    if not value or len(value) > 512:
        raise ValueError("invalid workspace session_id")
    pathmod = ntpath if ntpath.splitdrive(root)[0] else posixpath
    return pathmod.join(root, ".sessions", hashlib.sha256(value.encode("utf-8")).hexdigest())


# Backend-side view of the sandbox workspace root. Stays ``/workspace`` for the
# Docker sidecar / opensandbox / cube containers (in-container absolute path). The
# no-Docker local profile runs script_runner as a host subprocess pointed at a
# real dir (e.g. ``~/.hugagent/workspace``) via ``SCRIPT_RUNNER_WORKSPACE`` — the
# CLI exports it once so both the backend and the sidecar child agree. Kept as the
# single source for every model-facing ``/workspace`` mention and path we build.
WORKSPACE = os.getenv("SCRIPT_RUNNER_WORKSPACE", "/workspace")
