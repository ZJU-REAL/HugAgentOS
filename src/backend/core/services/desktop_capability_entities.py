"""Public agent/plugin declarations, account manifests and definition bundles."""

from __future__ import annotations

import copy
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from core.db.engine import SessionLocal
from core.services.desktop_capability_configs import _EFFECTIVE_TTL_S, _effective_lock
from core.services.desktop_capability_credentials import CapabilityContentRejected

# ── 智能体 / 插件清单与定义包（云端侧） ──────────────────────────────────
#
# 两类都是纯定义（没有脚本、没有二进制），定义体随清单哈希发布，正文按
# bundle 下发；本机侧按同一哈希核对后落到 R/agents、R/plugins 的 profile 目录。
# 模型服务密钥、企业上游地址永远不进这些文件。

_AGENT_PUBLIC_KEYS = (
    "agent_id",
    "owner_type",
    "name",
    "avatar",
    "description",
    "welcome_message",
    "suggested_questions",
    "mcp_server_ids",
    "skill_ids",
    "plugin_ids",
    "kb_ids",
    "model_provider_id",
    "temperature",
    "max_tokens",
    "max_iters",
    "timeout",
    "is_enabled",
    "sort_order",
    "source_market_slug",
    "ontology_tags",
    "version",
)
_entity_manifest_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}


def _zip_files(root: str, files: Dict[str, str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, body in sorted(files.items()):
            zf.writestr(f"{root}/{rel}", body)
    return buf.getvalue()


_DECLARATION_ENTRY_KEYS = frozenset(
    {
        "kind",
        "id",
        "key",
        "skill_id",
        "agent_id",
        "server_id",
        "required",
        "version_constraint",
        "version",
        "platforms",
        "platform",
        "execution_plane",
        "architecture",
        "python_version",
        "node_version",
    }
)
_RUNTIME_CONSTRAINT_KEYS = (
    "platforms",
    "platform",
    "execution_plane",
    "architecture",
    "python_version",
    "node_version",
)


def _declaration_atom(value: Any, depth: int = 0) -> Any:
    """Declarations contain scalar metadata, never arbitrary connection objects."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list) and depth < 4:
        return [_declaration_atom(item, depth + 1) for item in value]
    raise CapabilityContentRejected()


def _declaration_entry(value: Any) -> Any:
    if isinstance(value, str):
        return value  # Legacy component ID / runtime package requirement.
    if not isinstance(value, dict):
        raise CapabilityContentRejected()
    return {
        key: _declaration_atom(item)
        for key, item in value.items()
        if key in _DECLARATION_ENTRY_KEYS
    }


def _declaration_entries(values: Any) -> List[Any]:
    if values is None:
        return []
    return [
        _declaration_entry(value) for value in (values if isinstance(values, list) else [values])
    ]


def _declaration_groups(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise CapabilityContentRejected()
    # Unknown groups retain their identities and required flags so the resolver
    # reports unsupported required components instead of making them disappear.
    return {
        str(group): _declaration_entries(entries)
        for group, entries in value.items()
        if group != "warnings"
    }


def _public_declarations(value: Dict[str, Any]) -> Dict[str, Any]:
    public: Dict[str, Any] = {}
    for key in _RUNTIME_CONSTRAINT_KEYS:
        if key in value:
            public[key] = _declaration_atom(value[key])
    if "dependencies" in value:
        deps = value["dependencies"]
        public["dependencies"] = (
            _declaration_groups(deps) if isinstance(deps, dict) else _declaration_entries(deps)
        )
    if "components" in value:
        public["components"] = _declaration_groups(value["components"])
    if "extensions" in value:
        extensions = value["extensions"]
        if isinstance(extensions, dict):
            public["extensions"] = {
                key: (
                    _declaration_atom(item)
                    if key in _RUNTIME_CONSTRAINT_KEYS
                    else (
                        _declaration_entry(item) if isinstance(item, dict) else {"required": True}
                    )
                )
                for key, item in extensions.items()
            }
        else:
            public["extensions"] = _declaration_entries(extensions)
    for key in ("hooks", "rules", "commands"):
        if key in value:
            public[key] = _declaration_entries(value[key])
    return public


def _public_agent_extra(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    public = {
        key: _declaration_atom(value[key]) for key in ("version", "ontology_tags") if key in value
    }
    if "capability_requirements" in value:
        requirements = value["capability_requirements"]
        public["capability_requirements"] = (
            _public_declarations(requirements)
            if isinstance(requirements, dict)
            else _declaration_entries(requirements)
        )
    return public


def _agent_files(serialized: Dict[str, Any]) -> Dict[str, str]:
    definition = {k: serialized.get(k) for k in _AGENT_PUBLIC_KEYS}
    definition.update(_public_declarations(serialized))
    definition["extra_config"] = _public_agent_extra(serialized.get("extra_config"))
    return {
        "agent.json": json.dumps(definition, ensure_ascii=False, sort_keys=True, indent=2),
        "instructions.md": str(serialized.get("system_prompt") or ""),
    }


def _plugin_files(installed: Dict[str, Any]) -> Dict[str, str]:
    if installed.get("_uploaded_files") is not None:
        return dict(installed["_uploaded_files"])
    definition = {
        "install_id": installed["install_id"],
        "slug": installed["slug"],
        "name": installed["name"],
        "version": installed.get("version") or "",
        "description": installed.get("description") or "",
        "category": installed.get("category") or "",
        "icon": installed.get("icon"),
        "components": _declaration_groups(
            installed.get("components")
            or {key: installed.get(key) or [] for key in ("skills", "agents", "mcp", "plugins")}
        ),
        "ui_contributions": installed.get("ui_contributions"),
        "import_report": installed.get("import_report") or {},
    }
    definition.update(_public_declarations(installed))
    return {"plugin.json": json.dumps(definition, ensure_ascii=False, sort_keys=True, indent=2)}


def _user_agents(user_id: str) -> List[Dict[str, Any]]:
    from core.services.user_agent_service import UserAgentService

    with SessionLocal() as db:
        return list(UserAgentService(db).list_for_user(user_id) or [])


def _user_plugins(user_id: str) -> List[Dict[str, Any]]:
    from core.db.models import InstalledPlugin
    from core.services import plugin_service

    with SessionLocal() as db:
        from core.services.capability_workcopies import active_plugin_files

        rows = plugin_service.list_installed(db, user_id, include_global=True)
        metadata = {
            r.install_id: {
                "ui_contributions": r.ui_contributions,
                "components": r.component_ids or {},
                "_uploaded_files": active_plugin_files(db, user_id, r),
            }
            for r in db.query(InstalledPlugin).filter(
                InstalledPlugin.install_id.in_([r["install_id"] for r in rows] or [""])
            )
        }
    for r in rows:
        r.update(metadata.get(r["install_id"], {}))
    return rows


def build_user_agent_manifest(user_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    from core.services.desktop_capability_protocol import build_entity_manifest, entity_content_hash

    uid = str(user_id)
    now = time.monotonic()
    if use_cache:
        with _effective_lock:
            hit = _entity_manifest_cache.get(("agent", uid))
            if hit and (now - hit[0]) < _EFFECTIVE_TTL_S:
                return copy.deepcopy(hit[1])
    entries = [
        {
            "agent_id": a["agent_id"],
            "name": a["name"],
            "description": a.get("description") or "",
            "version": str(a.get("version") or ""),
            "content_hash": entity_content_hash(_agent_files(a)),
            "is_enabled": bool(a.get("is_enabled", True)),
        }
        for a in _user_agents(uid)
    ]
    manifest = build_entity_manifest("agent", entries)
    with _effective_lock:
        _entity_manifest_cache[("agent", uid)] = (now, copy.deepcopy(manifest))
    return manifest


def resolve_agent_bundle(user_id: str, agent_id: str) -> Optional[Tuple[bytes, str]]:
    from core.services.desktop_capability_protocol import entity_content_hash

    for a in _user_agents(str(user_id)):
        if a["agent_id"] == agent_id:
            files = _agent_files(a)
            return _zip_files(agent_id, files), entity_content_hash(files)
    return None


def build_user_plugin_manifest(user_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    from core.services.desktop_capability_protocol import build_entity_manifest, entity_content_hash

    uid = str(user_id)
    now = time.monotonic()
    if use_cache:
        with _effective_lock:
            hit = _entity_manifest_cache.get(("plugin", uid))
            if hit and (now - hit[0]) < _EFFECTIVE_TTL_S:
                return copy.deepcopy(hit[1])
    entries = [
        {
            "install_id": p["install_id"],
            "slug": p["slug"],
            "name": p["name"],
            "version": str(p.get("version") or ""),
            "description": p.get("description") or "",
            "category": p.get("category") or "",
            "content_hash": entity_content_hash(_plugin_files(p)),
            "enabled": bool(p.get("enabled", True)),
            "skills": list(p.get("skills") or []),
            "mcp": list(p.get("mcp") or []),
        }
        for p in _user_plugins(uid)
    ]
    manifest = build_entity_manifest("plugin", entries)
    with _effective_lock:
        _entity_manifest_cache[("plugin", uid)] = (now, copy.deepcopy(manifest))
    return manifest


def resolve_plugin_bundle(user_id: str, install_id: str) -> Optional[Tuple[bytes, str]]:
    from core.services.desktop_capability_protocol import entity_content_hash

    for p in _user_plugins(str(user_id)):
        if p["install_id"] == install_id:
            files = _plugin_files(p)
            return _zip_files(p["slug"], files), entity_content_hash(files)
    return None
