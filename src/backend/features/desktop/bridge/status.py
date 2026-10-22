"""status responsibilities."""
from __future__ import annotations
from typing import Any, Dict
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity

def initial_sync_status() -> Dict[str, Any]:
    """Report actual package counts without disk hashing or network probes."""
    from features.desktop.bridge import skills as desktop_cloud_skills, bundles as desktop_cloud_bundles
    from features.desktop.capabilities import session_availability
    from features.desktop.capabilities.paths import revision_for_hash

    st = bridge_identity.get_state()
    if not st:
        return {"ready": False, "syncing": False, "partial": False, "groups": [],
                "pending": 0, "error": "云端身份尚未就绪", "can_continue": False}
    with bridge_client_state.manifest_lock:
        manifest_ready = bridge_client_state.manifest is not None
        fetching = bridge_client_state.manifest_fetching
        error = bridge_client_state.manifest_error
    groups = {"skill": desktop_cloud_skills.status(), **desktop_cloud_bundles.status()}
    pending, completed, total = 0, 0, 0
    progress = []
    for kind, group in groups.items():
        error = error or group.get("last_error")
        rows = [row for row in group.get("installations", []) if row.get("state") != "removed"]
        done = sum(bool(row.get("content_hash")) and row.get("state") == "ready"
                   and row.get("resolved_revision") == revision_for_hash(row["content_hash"])
                   for row in rows)
        failed = sum(row.get("state") == "failed" or bool(row.get("last_error")) for row in rows)
        completed += done
        total += len(rows)
        pending += len(rows) - done
        progress.append({"kind": kind, "total": len(rows), "completed": done,
                         "failed": failed, "manifest_ready": bool(group.get("revision"))})
    complete_manifests = all(group.get("revision") for group in groups.values())
    partial = session_availability.active(desktop_cloud_skills._profile(st))
    # Progress is read without the identity lock: package rows may wait on the
    # database, and the shell's token push must never queue behind that wait.
    bridge_identity.require_current_account(st)
    complete = (manifest_ready and not fetching and not error and pending == 0 and complete_manifests)
    if not fetching and not complete and not partial:
        error = error or "部分能力未能同步完成，请选择重试或使用已同步能力继续"
    return {"ready": bool(complete or partial), "syncing": fetching, "partial": partial,
            "groups": progress, "completed": completed, "total": total,
            "totals_known": bool(complete_manifests), "pending": pending,
            "error": error, "can_continue": bool(manifest_ready and not fetching and not complete)}


def bridge_status() -> Dict[str, Any]:
    """诊断视图（/v1/desktop/capability/cloud-bridge/status）。"""
    from features.desktop.bridge import skills as desktop_cloud_skills

    st = bridge_identity.get_state()
    with bridge_client_state.manifest_lock:
        servers = (bridge_client_state.manifest or {}).get("servers") or []
        err = bridge_client_state.manifest_error
        manifest_revision = str((bridge_client_state.manifest or {}).get("revision") or "")
        schema_ready_count = sum(
            1 for server in servers if isinstance(server, dict) and bool(server.get("tools"))
        )
    return {
        "configured": st is not None,
        "cloud_base": (st or {}).get("cloud_base"),
        "active": bridge_identity.bridge_active(),
        "cloud_server_count": len(servers),
        "manifest_revision": manifest_revision,
        "schema_mode": "dynamic_manifest" if manifest_revision else "none",
        "schema_ready_server_count": schema_ready_count,
        "cloud_servers": [
            {
                "server_id": s.get("server_id"),
                "component": s.get("component"),
                "display_name": s.get("display_name"),
                "origin": "cloud",
            }
            for s in servers
            if isinstance(s, dict)
        ],
        "last_error": err,
        "skills": desktop_cloud_skills.status(),
        "bundles": bundles_status(),
        "mcp_json": mcp_json_status(),
    }


def bundles_status() -> Dict[str, Any]:
    from features.desktop.bridge import bundles as desktop_cloud_bundles

    return desktop_cloud_bundles.status()


def mcp_json_status() -> Dict[str, Any]:
    from features.desktop.capabilities import connectors, mcp_json
    from features.desktop.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return {"enabled": False}
    try:
        doc = mcp_json.load()
    except mcp_json.McpJsonCorrupt as exc:
        return {"enabled": True, "corrupt": True, "error": str(exc)}
    res = connectors.last_resolution()
    return {
        "enabled": True,
        "generation": doc.generation,
        "local_server_count": len(doc.local),
        "managed_profiles": {
            p: len((v or {}).get("servers") or {}) for p, v in doc.managed.items()
        },
        "conflicts": sorted(res.conflicts) if res else [],
        "shadowed": sorted(res.shadowed) if res else [],
    }
