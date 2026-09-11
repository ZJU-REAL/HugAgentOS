"""Persistent capability-SID store, ported from Codex's ``cap.rs`` (Apache-2.0).

One capability SID per writable root, remembered across runs. Persistence is not
an optimisation: the ACE written onto a folder names a specific SID, so the next
run has to present *that* SID to inherit the grant instead of re-ACLing the
folder with a fresh one every time.

Keying by root — rather than one global SID — is what keeps two authorized
folders from lending each other write access: a run that was granted folder A
carries A's SID only, so B's ACE matches nothing in its token.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

STORE_FILENAME = "sandbox_capability_sids.json"

# Reserved store key for the SID a read-only run carries. A restricted token
# needs at least one restricting SID, and this one is deliberately granted
# nowhere on the filesystem — so a policy with no writable roots produces a
# token that can write nothing at all.
READONLY_KEY = ":readonly"

_LOCK = threading.Lock()


def random_capability_sid() -> str:
    """Mint an unused capability SID.

    The shape matches Codex's: a domain-style SID with four random sub
    authorities. No domain ever issued it, so it cannot collide with a real
    principal and an ACE naming it is meaningful only to this sandbox.
    """
    return "S-1-5-21-" + "-".join(str(secrets.randbits(32)) for _ in range(4))


def store_path(state_dir: str) -> Path:
    return Path(state_dir) / STORE_FILENAME


def _canonical_key(path: str) -> str:
    """Stable key for a root: absolute, case-folded, separator-normalized.

    Windows paths are case-insensitive, so ``C:\\Repo`` and ``c:\\repo`` must not
    end up with two different capability SIDs and two different ACEs.
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _load(path: Path) -> dict[str, str]:
    """Read the store, treating an absent or unreadable one as empty.

    Starting over cannot widen anything, which is why it is safe to do quietly:
    the token a run carries holds exactly the SIDs this file was just written
    with, so a forgotten SID is one the sandbox no longer presents. The stale
    ACE it left on a folder then grants an identity nobody holds.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


def _save(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def capability_sids_for_roots(state_dir: str, roots: list[str]) -> dict[str, str]:
    """Capability SID for each root, minting and persisting the missing ones.

    Returns a mapping keyed by the *original* root strings so callers can pair
    each SID with the path it has to be ACLed onto.
    """
    return _sids_for_keys(state_dir, {root: _canonical_key(root) for root in roots})


def readonly_capability_sid(state_dir: str) -> str:
    """The SID a run with no writable roots carries. Granted nowhere, ever."""
    return _sids_for_keys(state_dir, {READONLY_KEY: READONLY_KEY})[READONLY_KEY]


def _sids_for_keys(state_dir: str, keys_by_name: dict[str, str]) -> dict[str, str]:
    path = store_path(state_dir)
    result: dict[str, str] = {}
    with _LOCK:
        stored = _load(path)
        dirty = False
        for name, key in keys_by_name.items():
            sid = stored.get(key)
            if sid is None:
                sid = random_capability_sid()
                stored[key] = sid
                dirty = True
            result[name] = sid
        if dirty:
            _save(path, stored)
    return result


__all__ = [
    "READONLY_KEY",
    "STORE_FILENAME",
    "capability_sids_for_roots",
    "random_capability_sid",
    "readonly_capability_sid",
    "store_path",
]
