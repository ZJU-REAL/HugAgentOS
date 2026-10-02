"""Pin connector and agent identities for durable replay."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from typing import Optional

from core.capabilities import registry, skills
from core.capabilities.errors import PermissionDenied
from core.capabilities.paths import LOCAL_PROFILE
from core.capabilities.runtime import state as runtime
from core.db.models import ContentBlock


def _config_digest(config):
    # Connection instructions are frozen; secret inputs may rotate independently.
    secret_names = (
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "API_KEY",
        "APIKEY",
        "CREDENTIAL",
        "AUTHORIZATION",
        "COOKIE",
    )

    def public_values(values):
        return {
            key: value
            for key, value in (values or {}).items()
            if not any(marker in str(key).upper().replace("-", "_") for marker in secret_names)
        }

    safe = {
        key: val
        for key, val in config.items()
        if key not in {"headers", "env", "manifest_tools", "manifest_revision", "schema_hash"}
    }
    safe["headers"] = public_values(config.get("headers"))
    safe["env"] = public_values(config.get("env"))
    return hashlib.sha256(json.dumps(safe, sort_keys=True, default=str).encode()).hexdigest()


def bind_mcp(run: runtime.PreparedRun, configs, choices):
    """Freeze MCP source identities/contracts; refresh credentials independently."""
    from core.capabilities.connectors import server_id_of

    selected = {server_id_of(c): c.install_id for c in choices.chosen.values()} if choices else {}
    current = {
        sid: {
            "install_id": selected.get(sid, "mcp:local:" + sid),
            "authorization_checked": sid in selected,
            "config_digest": _config_digest(config),
            "manifest_tools": copy.deepcopy(config.get("manifest_tools")),
            "schema_hash": config.get("schema_hash"),
            "manifest_revision": config.get("manifest_revision"),
        }
        for sid, config in configs.items()
    }
    with runtime._lock:
        saved = runtime.get(run.run_id, scope_id=run.scope_id)
        runtime.validate(saved or run)
        pinned = (saved or run).mcp_bindings
        if not (saved or run).mcp_frozen:
            pinned = current
            frozen = replace(saved or run, mcp_bindings=pinned, mcp_frozen=True)
            if any(
                entry["install_id"].split(":", 2)[1] not in (LOCAL_PROFILE, "local-json")
                for entry in pinned.values()
            ):
                frozen = runtime._bind_cloud_identity(frozen)
            with registry._session() as db:
                row = db.get(
                    ContentBlock, runtime._PREFIX + runtime._snapshot_key(run.run_id, run.scope_id)
                )
                row.payload = frozen.to_dict()
        else:
            # A sub-agent may use a subset; expanding outside the parent's
            # frozen bindings requires a new run, not an implicit source swap.
            unavailable = dict((saved or run).unavailable)
            for sid, entry in list(current.items()):
                old = pinned.get(sid)
                if (
                    old is None
                    or old["install_id"] != entry["install_id"]
                    or old["config_digest"] != entry["config_digest"]
                ):
                    # Keep the old source contract; never reconnect a changed
                    # endpoint as though it were the original tool.
                    unavailable["mcp:" + sid] = "connector_changed"
                    configs = {key: value for key, value in configs.items() if key != sid}
            if unavailable != (saved or run).unavailable:
                runtime.save(replace(saved or run, unavailable=unavailable))
        # Pinned schemas are persisted and never edited afterwards; sharing them
        # with the assembled config is safe and avoids re-copying every tool schema.
        return {
            sid: {
                **config,
                **{
                    key: pinned[sid][key]
                    for key in ("manifest_tools", "schema_hash", "manifest_revision")
                    if pinned[sid].get(key) is not None
                },
            }
            for sid, config in configs.items()
        }


def pin_agent_definition(run_id, user_id, definition, *, scope_id: str = ""):
    """Keep the selected agent's instructions and dependency IDs stable on replay."""
    from core.capabilities.agents import _JSON_FIELDS, AgentDefinition

    ident = str(definition.agent_id)
    key = "desktop_capability_agent:" + runtime._snapshot_key(str(run_id) + ":" + ident, scope_id)
    profile = (
        skills.current_account_profile()
        if getattr(definition, "origin", "local") == "cloud"
        else None
    )
    from core.services.desktop_cloud_bridge import (
        _state_fingerprint,
        ensure_current_authorization,
        get_state,
    )

    fingerprint = _state_fingerprint(get_state()) if profile else None
    if getattr(definition, "origin", "local") == "cloud":
        if not skills.account_authorized_for(user_id):
            raise PermissionDenied("cloud agent belongs to another user")
        ensure_current_authorization()
        current = registry.get(
            registry.install_id("agent", str(getattr(definition, "profile", profile)), ident)
        )
        if current is None or not current.ready or not current.enabled:
            raise PermissionDenied("cloud agent is no longer authorized")
    if not getattr(definition, "is_enabled", True):
        raise PermissionDenied("selected agent is disabled")
    with runtime._lock, registry._session() as db:
        row = db.get(ContentBlock, key)
        if row is not None:
            data = copy.deepcopy(row.payload)
            if (
                data["user_id"] != str(user_id)
                or data["profile"] != profile
                or data.get("authorization_fingerprint") != fingerprint
            ):
                raise PermissionDenied("prepared agent belongs to another account")
            return AgentDefinition.from_serialized(data["definition"])
        fields = {
            name: copy.deepcopy(getattr(definition, name, None))
            for name in _JSON_FIELDS
            if hasattr(definition, name)
        }
        fields.update(
            {
                "system_prompt": str(getattr(definition, "system_prompt", "") or ""),
                "user_id": getattr(definition, "user_id", None),
                "origin": getattr(definition, "origin", "local"),
                "profile": getattr(definition, "profile", LOCAL_PROFILE),
                "revision": getattr(definition, "revision", None),
            }
        )
        frozen = AgentDefinition.from_serialized(fields)
        db.add(
            ContentBlock(
                id=key,
                payload={
                    "run_id": str(run_id),
                    "scope_id": str(scope_id or ""),
                    "user_id": str(user_id),
                    "profile": profile,
                    "authorization_fingerprint": fingerprint,
                    "definition": fields,
                },
            )
        )
        return frozen


def references(kind: str, profile: str, key: str, revision: Optional[str] = None) -> list[str]:
    """History retains exact skill, plugin and agent revisions until explicitly purged."""
    target = registry.install_id(kind, profile, key)
    with registry._session() as db:
        rows = db.query(ContentBlock).filter(ContentBlock.id.startswith(runtime._PREFIX)).all()
        result = []
        for row in rows:
            entries = list((row.payload.get("bindings") or {}).values()) + (
                row.payload.get("dependency_report") or {}
            ).get("nodes", [])
            if any(
                entry.get("install_id") == target
                and (revision is None or entry.get("revision") == revision)
                for entry in entries
            ):
                result.append(str(row.payload["run_id"]))
        if kind == "agent":
            rows = (
                db.query(ContentBlock)
                .filter(ContentBlock.id.startswith("desktop_capability_agent:"))
                .all()
            )
            for row in rows:
                data = row.payload.get("definition") or {}
                if (
                    data.get("agent_id") == key
                    and data.get("profile", LOCAL_PROFILE) == profile
                    and (revision is None or data.get("revision") == revision)
                ):
                    if row.payload.get("run_id"):
                        result.append(str(row.payload["run_id"]))
        return sorted(set(result))
