"""Versioned desktop capability-manifest contract shared by cloud and local runtimes.

Tool definitions are never compiled into the desktop application. The cloud builds
this document from the current database state, while the local runtime validates and
caches that document as an immutable snapshot. A schema or authorization change
produces a new revision and is revalidated by the cloud again at invocation time.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from typing import Any, Dict, List, Optional

CAPABILITY_MANIFEST_VERSION = 2

_PUBLIC_TOOL_KEYS = (
    "name",
    "title",
    "description",
    "inputSchema",
    "outputSchema",
)
_PUBLIC_ANNOTATION_KEYS = (
    "title",
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)


class CapabilityManifestError(ValueError):
    """Raised when the cloud/local capability contract is incomplete or stale."""


class CapabilityManifestStaleError(CapabilityManifestError):
    """Raised when an invocation references an out-of-date schema snapshot."""


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def token_subject(token: str) -> str:
    """Read the user id a capability token was issued for, without verifying it.

    Only the cloud can verify a token. The local runtime uses this to tell a
    routine token rotation (same account) apart from an account switch.
    """
    try:
        _prefix, body, _sig = (token or "").strip().split(".", 2)
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return str(payload.get("u") or "").strip()
    except Exception:  # noqa: BLE001 - opaque tokens fall back to raw comparison
        return ""


def public_tool_schema(raw: Any) -> Optional[Dict[str, Any]]:
    """Return the credential-free MCP tool contract allowed to leave the cloud."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    schema = raw.get("inputSchema") or raw.get("input_schema") or {}
    if not isinstance(schema, dict):
        schema = {}
    schema = dict(schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    public: Dict[str, Any] = {
        "name": name,
        "description": str(raw.get("description") or ""),
        "inputSchema": schema,
    }
    for key in _PUBLIC_TOOL_KEYS:
        if key in {"name", "description", "inputSchema"}:
            continue
        value = raw.get(key)
        if value is not None and isinstance(value, (str, dict, list, bool, int, float)):
            public[key] = value
    annotations = raw.get("annotations")
    if isinstance(annotations, dict):
        safe_annotations = {
            key: annotations[key]
            for key in _PUBLIC_ANNOTATION_KEYS
            if key in annotations and isinstance(annotations[key], (str, bool))
        }
        if safe_annotations:
            public["annotations"] = safe_annotations
    return public


def public_tool_schemas(raw_tools: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_tools, list):
        return []
    return [tool for raw in raw_tools if (tool := public_tool_schema(raw)) is not None]


def build_manifest(servers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Seal a cloud-generated server list into the current protocol envelope."""
    snapshot = copy.deepcopy(servers)
    return {
        "version": CAPABILITY_MANIFEST_VERSION,
        "revision": canonical_hash(snapshot),
        "servers": snapshot,
    }


def validate_manifest(raw: Any) -> Dict[str, Any]:
    """Validate a dynamic cloud snapshot without accepting legacy wire shapes."""
    if not isinstance(raw, dict):
        raise CapabilityManifestError("manifest must be an object")
    if raw.get("version") != CAPABILITY_MANIFEST_VERSION:
        raise CapabilityManifestError("unsupported capability manifest version")
    servers = raw.get("servers")
    revision = str(raw.get("revision") or "")
    if not isinstance(servers, list) or not revision:
        raise CapabilityManifestError("manifest is missing servers or revision")

    normalized: List[Dict[str, Any]] = []
    for raw_server in servers:
        if not isinstance(raw_server, dict):
            raise CapabilityManifestError("manifest server must be an object")
        server = copy.deepcopy(raw_server)
        server_id = str(server.get("server_id") or "").strip()
        tools = server.get("tools")
        if not server_id or not isinstance(tools, list):
            raise CapabilityManifestError("manifest server is incomplete")
        for tool in tools:
            if public_tool_schema(tool) != tool:
                raise CapabilityManifestError("manifest tool schema is incomplete")
        expected_schema_hash = canonical_hash(tools)
        if str(server.get("schema_hash") or "") != expected_schema_hash:
            raise CapabilityManifestError("manifest server schema hash mismatch")
        server["server_id"] = server_id
        normalized.append(server)

    if canonical_hash(normalized) != revision:
        raise CapabilityManifestError("manifest revision mismatch")
    return {
        "version": CAPABILITY_MANIFEST_VERSION,
        "revision": revision,
        "servers": normalized,
    }


# ── Skill manifest ───────────────────────────────────────────────────────

SKILL_MANIFEST_VERSION = 1

_SKILL_ENTRY_KEYS = frozenset(
    {
        "skill_id",
        "display_name",
        "description",
        "version",
        "scope",
        "content_hash",
        "mcp_server_ids",
    }
)


def skill_content_hash(skill_content: str, extra_files: Dict[str, Any]) -> str:
    """Hash one skill snapshot: SKILL.md plus every extra file, order-independent."""
    files = {str(k): str(v) for k, v in (extra_files or {}).items()}
    files["SKILL.md"] = str(skill_content or "")
    return canonical_hash(files)


def build_skill_manifest(skills: List[Dict[str, Any]], suppressed_ids: List[str]) -> Dict[str, Any]:
    """Seal the user's effective skill set plus cloud-known but unavailable ids."""
    snapshot = copy.deepcopy(skills)
    suppressed = sorted({str(x) for x in suppressed_ids if str(x).strip()})
    return {
        "version": SKILL_MANIFEST_VERSION,
        "revision": canonical_hash({"skills": snapshot, "suppressed_ids": suppressed}),
        "skills": snapshot,
        "suppressed_ids": suppressed,
    }


def validate_skill_manifest(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise CapabilityManifestError("skill manifest must be an object")
    if raw.get("version") != SKILL_MANIFEST_VERSION:
        raise CapabilityManifestError("unsupported skill manifest version")
    skills = raw.get("skills")
    suppressed = raw.get("suppressed_ids")
    revision = str(raw.get("revision") or "")
    if not isinstance(skills, list) or not isinstance(suppressed, list) or not revision:
        raise CapabilityManifestError(
            "skill manifest is missing skills, suppressed_ids or revision"
        )

    normalized: List[Dict[str, Any]] = []
    seen: set = set()
    for raw_skill in skills:
        if not isinstance(raw_skill, dict) or set(raw_skill) != _SKILL_ENTRY_KEYS:
            raise CapabilityManifestError("skill manifest entry is incomplete")
        skill_id = str(raw_skill.get("skill_id") or "").strip()
        if not skill_id or skill_id in seen or len(str(raw_skill.get("content_hash") or "")) != 64:
            raise CapabilityManifestError("skill manifest entry has an invalid id or hash")
        if raw_skill.get("scope") not in ("shared", "private"):
            raise CapabilityManifestError("skill manifest entry has an invalid scope")
        if not isinstance(raw_skill.get("mcp_server_ids"), list):
            raise CapabilityManifestError("skill manifest entry has invalid mcp_server_ids")
        seen.add(skill_id)
        normalized.append(copy.deepcopy(raw_skill))
    suppressed_ids = [str(x) for x in suppressed]
    if suppressed_ids != sorted(set(suppressed_ids)):
        raise CapabilityManifestError("skill manifest suppressed_ids must be sorted and unique")
    if canonical_hash({"skills": normalized, "suppressed_ids": suppressed_ids}) != revision:
        raise CapabilityManifestError("skill manifest revision mismatch")
    return {
        "version": SKILL_MANIFEST_VERSION,
        "revision": revision,
        "skills": normalized,
        "suppressed_ids": suppressed_ids,
    }
