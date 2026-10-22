"""An explicit partial-sync choice narrows this desktop login's cloud revisions.

The immutable snapshot is replaced atomically. It never grants access or changes
cloud/device preferences; normal owner, enabled and integrity checks still apply.
"""
from types import MappingProxyType

_snapshot = None


def activate(profile, revisions):
    global _snapshot
    _snapshot = (str(profile), MappingProxyType(dict(revisions)))


def clear():
    global _snapshot
    _snapshot = None


def active(profile=None):
    snapshot = _snapshot
    return snapshot is not None and (profile is None or snapshot[0] == profile)


def permits(profile, install_id, revision):
    snapshot = _snapshot
    if install_id.startswith("mcp:"):
        return True  # MCP schemas arrive in the tool manifest, not file packages.
    if snapshot is None or snapshot[0] != profile:
        return True
    return snapshot[1].get(install_id) == revision and revision is not None
