"""Read-only, account-scoped desktop update detection.

A global cache epoch is not evidence of a change in this account. Compare the
actual manifests with the accepted local snapshots; never reconcile or download.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_bundles as bundles
from core.services import desktop_cloud_skills as skills
from core.services.desktop_capability_protocol import (
    canonical_hash,
    validate_manifest,
    validate_skill_manifest,
    validate_entity_manifest,
)

_lock = threading.Lock()
_cache_key = ""
_cache_until = 0.0
_cache_result: dict[str, Any] = {"changed": None}
CHECK_INTERVAL_SECONDS = 60.0


def _snapshots():
    return {
        "mcp": bridge.synced_manifest(),
        "skill": skills.synced_manifest(),
        **bundles.synced_manifests(),
    }


def _content(manifest):
    # Initial cloud toggles do not override this device's choices. Only remove
    # top-level entry flags: tool schemas and nested definitions remain exact.
    entries = manifest.get("servers", manifest.get("skills", manifest.get("entries", [])))
    return sorted(
        canonical_hash({k: v for k, v in entry.items() if k not in ("enabled", "is_enabled")})
        for entry in entries
    )


def _fetch(st, kind, known):
    path = {
        "mcp": "manifest",
        "skill": "skills/manifest",
        "agent": "agents/manifest",
        "plugin": "plugins/manifest",
    }[kind]
    response = httpx.get(
        f"{st['cloud_base']}/api/v1/desktop/capability/{path}",
        headers={
            **bridge.cloud_headers(st),
            "Cache-Control": "no-cache",
            "If-None-Match": f'"{known["revision"]}"',
        },
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    bridge.require_current_account(st)
    if response.status_code == 304:
        return known
    response.raise_for_status()
    raw = response.json().get("data")
    if kind == "mcp":
        return validate_manifest(raw)
    if kind == "skill":
        return validate_skill_manifest(raw)
    return validate_entity_manifest(raw, kind)


def check(st: dict[str, Any]) -> dict[str, Any]:
    """Return changed=None when comparison is unavailable, never a false update."""
    global _cache_key, _cache_until, _cache_result
    with _lock:
        with bridge.account_scope(st):
            local = _snapshots()
            if any(snapshot is None for snapshot in local.values()):
                return {"changed": None}
            key = canonical_hash([bridge._state_fingerprint(st), local])
            if key == _cache_key and time.monotonic() < _cache_until:
                return dict(_cache_result)
        try:
            changed = any(
                [
                    _content(_fetch(st, kind, manifest)) != _content(manifest)
                    for kind, manifest in local.items()
                ]
            )
            # Automatic recovery or another window may have synchronized while
            # the comparison was in flight. Do not publish against an old base.
            with bridge.account_scope(st):
                if local != _snapshots():
                    return {"changed": None}
                result = {"changed": changed}
        except Exception:
            # Network/protocol failures are unknown, not an update. No credentials
            # or raw upstream error bodies are exposed by this advisory endpoint.
            bridge.require_current_account(st)
            result = {"changed": None}
        _cache_key, _cache_result = key, result
        _cache_until = time.monotonic() + CHECK_INTERVAL_SECONDS
        return dict(result)
