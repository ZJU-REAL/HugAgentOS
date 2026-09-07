"""Connector (MCP) kind: candidates by component name, explicit binding choice.

A connector is called by ``server_id``; the resolver decides, per *component*
name, which binding this device uses: the cloud gateway (the bridged account's
manifest), a device catalog row, or a ``mcp.json`` local declaration. The cloud
binding is the account-level candidate and wins a same-component clash unless
the user chose otherwise (name preference) or the deployment keeps that
component local (``DESKTOP_LOCAL_MCP_KEEP``). Nothing is silently replaced:
the losing binding is reported as shadowed.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from . import registry
from .paths import KIND_MCP, LOCAL_PROFILE
from .resolver import SOURCE_CLOUD, SOURCE_LOCAL, Candidate, Resolution, resolve

MCP_JSON_PROFILE = "local-json"
_DB_PATH = Path("<db>")

_lock = threading.Lock()
_last: Optional[Resolution] = None


def db_candidates(base_map: Dict[str, str], enabled_ids: Set[str]) -> List[Candidate]:
    """Device catalog rows: ``server_id → component base name``."""
    out: List[Candidate] = []
    for sid, base in base_map.items():
        out.append(
            Candidate(
                install_id=registry.install_id(KIND_MCP, LOCAL_PROFILE, sid),
                runtime_name=base,
                kind=KIND_MCP,
                profile=LOCAL_PROFILE,
                source=SOURCE_LOCAL,
                path=_DB_PATH,
                usable=sid in enabled_ids,
                account_level=False,
                display_name=sid,
                state="ready" if sid in enabled_ids else "disabled",
            )
        )
    return out


def cloud_candidates(
    profile: str, servers: Iterable[Dict[str, Any]], enabled: Dict[str, bool]
) -> List[Candidate]:
    out: List[Candidate] = []
    for s in servers:
        sid = str(s["server_id"])
        on = enabled.get(sid, True)
        has_schema = bool(s.get("tools"))
        out.append(
            Candidate(
                install_id=registry.install_id(KIND_MCP, profile, sid),
                runtime_name=str(s.get("component") or sid),
                kind=KIND_MCP,
                profile=profile,
                source=SOURCE_CLOUD,
                path=_DB_PATH,
                content_hash=str(s.get("schema_hash") or "") or None,
                usable=on and has_schema,
                account_level=on,
                display_name=str(s.get("display_name") or sid),
                description=str(s.get("description") or ""),
                state=("ready" if has_schema else "schema_empty") if on else "disabled",
            )
        )
    return out


def json_candidates(local_servers: Dict[str, Dict[str, Any]]) -> List[Candidate]:
    out: List[Candidate] = []
    for sid, entry in local_servers.items():
        on = bool(entry.get("enabled", True))
        out.append(
            Candidate(
                install_id=registry.install_id(KIND_MCP, MCP_JSON_PROFILE, sid),
                runtime_name=sid,
                kind=KIND_MCP,
                profile=MCP_JSON_PROFILE,
                source=SOURCE_LOCAL,
                path=_DB_PATH,
                usable=on,
                account_level=False,
                display_name=str(entry.get("displayName") or sid),
                description=str(entry.get("description") or ""),
                state="ready" if on else "disabled",
            )
        )
    return out


def server_id_of(candidate: Candidate) -> str:
    return candidate.install_id.split(":", 2)[2]


def resolve_bindings(
    candidates: List[Candidate], *, keep_local: Set[str], user_id: Optional[str] = None
) -> Resolution:
    """Per component name: keep-local components never take the cloud binding."""
    filtered = [
        c for c in candidates if not (c.source == SOURCE_CLOUD and c.runtime_name in keep_local)
    ]
    from .skills import current_local_user_id

    selected_user_id = user_id if user_id is not None else current_local_user_id()
    res = resolve(
        KIND_MCP, filtered, preferences=registry.preferences(KIND_MCP, user_id=selected_user_id)
    )
    with _lock:
        global _last
        _last = res
    return res


def last_resolution() -> Optional[Resolution]:
    with _lock:
        return _last
