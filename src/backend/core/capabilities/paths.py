"""Where the desktop capability store lives.

``HUGAGENT_CAPS_ROOT`` is injected by the desktop shell (Windows:
``%LOCALAPPDATA%\\<app identifier>``) and defaulted by the local CLI profile to
``HUGAGENT_HOME``. It is *unset* on cloud deployments, which disables the file
store entirely — every function here that needs the root raises when it is
missing rather than guessing a location.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

CAPS_ROOT_ENV = "HUGAGENT_CAPS_ROOT"

KIND_SKILL = "skill"
KIND_PLUGIN = "plugin"
KIND_AGENT = "agent"
KIND_MCP = "mcp"
KINDS = (KIND_SKILL, KIND_PLUGIN, KIND_AGENT, KIND_MCP)

_KIND_DIRS = {KIND_SKILL: "skills", KIND_PLUGIN: "plugins", KIND_AGENT: "agents"}

LOCAL_PROFILE = "local"
BUILTIN_PROFILE = "builtin"

META_DIR = ".capabilities"
MCP_JSON_NAME = "mcp.json"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class CapabilityStoreDisabled(RuntimeError):
    """Raised when file-store operations run on a deployment without a root."""


def capability_root() -> Optional[Path]:
    raw = os.getenv(CAPS_ROOT_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def capabilities_enabled() -> bool:
    return capability_root() is not None


def require_root() -> Path:
    root = capability_root()
    if root is None:
        raise CapabilityStoreDisabled(
            f"{CAPS_ROOT_ENV} is not set; the capability file store is disabled"
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def assert_managed_path(path: Path) -> Path:
    """Store/index paths are real directories; links belong only in runtime views."""
    from .errors import IntegrityFailed
    from .junction import is_directory_link

    root = require_root().absolute()
    path = path.absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise IntegrityFailed("managed path is outside capability root") from exc
    current = root
    for part in parts:
        current = current / part
        if part == ".." or is_directory_link(current):
            raise IntegrityFailed(f"managed path is redirected: {current}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise IntegrityFailed("managed path resolves outside capability root")
    return path


def safe_segment(value: str) -> str:
    """A single path segment safe on every platform, or ``ValueError``."""
    seg = (value or "").strip()
    if seg != value or seg.endswith((".", " ")) or not _SEGMENT_RE.match(seg) or seg in (".", ".."):
        raise ValueError(f"unsafe path segment: {value!r}")
    if seg.split(".")[0].upper() in _WINDOWS_RESERVED:
        raise ValueError(f"reserved name: {value!r}")
    return seg


def kind_root(kind: str) -> Path:
    if kind not in _KIND_DIRS:
        raise ValueError(f"kind {kind!r} has no directory")
    path = assert_managed_path(require_root() / _KIND_DIRS[kind])
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_dir(kind: str, profile: str) -> Path:
    return assert_managed_path(kind_root(kind) / safe_segment(profile))


def component_dir(kind: str, profile: str, key: str, revision: str) -> Path:
    return assert_managed_path(
        profile_dir(kind, profile) / safe_segment(key) / safe_segment(revision)
    )


def mcp_json_path() -> Path:
    return assert_managed_path(require_root() / MCP_JSON_NAME)


def meta_root() -> Path:
    path = assert_managed_path(require_root() / META_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def staging_root() -> Path:
    path = assert_managed_path(meta_root() / "staging")
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifests_root() -> Path:
    path = assert_managed_path(meta_root() / "manifests")
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrations_root() -> Path:
    path = assert_managed_path(meta_root() / "migrations")
    path.mkdir(parents=True, exist_ok=True)
    return path


def revision_for_hash(content_hash: str) -> str:
    """Directory-safe revision derived from a content hash (immutable per content)."""
    h = (content_hash or "").strip().lower()
    if len(h) < 12 or not re.match(r"^[0-9a-f]+$", h):
        raise ValueError("content hash must be a hex digest")
    return h[:12]
