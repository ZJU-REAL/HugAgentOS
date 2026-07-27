"""Local-mode folder grants + danger-command policy store (ticket #06).

A desktop-local, single-user store of which host folders the agent is authorized
to touch (beyond the default workspace root) and how each danger-command class is
handled. Persisted as one JSON file under the local data dir (``~/.hugagent`` by
default), so it survives restarts and is shared between the shell (which writes
grants when the user authorizes a folder) and the backend (which reads them in
the execution policy gate).

Edition-agnostic shared ``core`` module; only used by the desktop local backend.
The gate integration and the routes are both gated on ``local_mode_enabled()``,
so the cloud/web deployment never reads or exposes any of this.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

_LOCK = threading.Lock()
_VALID_MODES = ("read", "readwrite")
_VALID_DISPOSITIONS = ("block", "confirm", "allow")
_DANGER_CATEGORIES = ("delete", "system_write", "network", "privilege")


def _data_dir() -> Path:
    return Path(os.getenv("HUGAGENT_HOME", str(Path.home() / ".hugagent"))).expanduser()


def _store_path() -> Path:
    return _data_dir() / "local_grants.json"


def _default_state() -> Dict[str, Any]:
    return {"grants": [], "policy": {}}


def _load() -> Dict[str, Any]:
    path = _store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        data.setdefault("grants", [])
        data.setdefault("policy", {})
        return data
    except (OSError, ValueError):
        return _default_state()


def _save(data: Dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_grants() -> List[Dict[str, Any]]:
    return list(_load().get("grants", []))


def add_grant(path: str, mode: str = "readwrite") -> Dict[str, Any]:
    real = os.path.abspath(os.path.expanduser((path or "").strip()))
    if not real:
        raise ValueError("授权路径不能为空")
    if mode not in _VALID_MODES:
        raise ValueError(f"mode 必须是 {_VALID_MODES}")
    with _LOCK:
        data = _load()
        grants = [g for g in data.get("grants", []) if g.get("path") != real]
        entry = {"path": real, "mode": mode}
        grants.append(entry)
        data["grants"] = grants
        _save(data)
    return entry


def remove_grant(path: str) -> None:
    real = os.path.abspath(os.path.expanduser((path or "").strip()))
    with _LOCK:
        data = _load()
        data["grants"] = [g for g in data.get("grants", []) if g.get("path") != real]
        _save(data)


def get_policy() -> Dict[str, str]:
    return dict(_load().get("policy", {}))


def set_policy(policy: Dict[str, str]) -> Dict[str, str]:
    clean: Dict[str, str] = {}
    for key, value in (policy or {}).items():
        if key == "out_of_scope" or key in _DANGER_CATEGORIES:
            if value not in _VALID_DISPOSITIONS:
                raise ValueError(f"{key} 的取值必须是 {_VALID_DISPOSITIONS}")
            clean[key] = value
    with _LOCK:
        data = _load()
        data["policy"] = clean
        _save(data)
    return clean


def grants_for_gate() -> List[Any]:
    """Convert stored grants into ``local_policy.Grant`` objects for the gate."""
    from core.sandbox.local_policy import Grant

    return [
        Grant(path=g["path"], mode=g.get("mode", "readwrite"))
        for g in list_grants()
        if g.get("path")
    ]


# ── Approval mode (Codex-style in-dialog permission bar, ticket #06+) ─────────
_VALID_APPROVAL = ("strict", "standard", "full")
_DEFAULT_APPROVAL = "standard"


def get_approval_mode() -> str:
    mode = _load().get("approval_mode", _DEFAULT_APPROVAL)
    return mode if mode in _VALID_APPROVAL else _DEFAULT_APPROVAL


def set_approval_mode(mode: str) -> str:
    if mode not in _VALID_APPROVAL:
        raise ValueError(f"approval_mode 必须是 {_VALID_APPROVAL}")
    with _LOCK:
        data = _load()
        data["approval_mode"] = mode
        _save(data)
    return mode


def policy_for_gate() -> Any:
    """Build the effective ``local_policy.Policy`` from the approval mode + stored dispositions.

    The approval mode is the coarse, in-dialog permission dial:
      - ``strict`` : block out-of-scope paths and every danger category (most cautious).
      - ``standard``: per-category policy / built-in defaults (the middle ground).
      - ``full``   : allow everything (only audit-logged) — hands-off autonomy.
    """
    from core.sandbox.local_policy import DELETE, NETWORK, PRIVILEGE, SYSTEM_WRITE, Policy

    mode = get_approval_mode()
    categories = (DELETE, SYSTEM_WRITE, NETWORK, PRIVILEGE)
    if mode == "strict":
        return Policy(out_of_scope="block", danger={c: "block" for c in categories})
    if mode == "full":
        return Policy(out_of_scope="allow", danger={c: "allow" for c in categories})
    stored = get_policy()
    out_of_scope = stored.get("out_of_scope", "confirm")
    danger = {k: v for k, v in stored.items() if k in _DANGER_CATEGORIES}
    return Policy(out_of_scope=out_of_scope, danger=danger)


__all__ = [
    "list_grants",
    "add_grant",
    "remove_grant",
    "get_policy",
    "set_policy",
    "get_approval_mode",
    "set_approval_mode",
    "grants_for_gate",
    "policy_for_gate",
]
