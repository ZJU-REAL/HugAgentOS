"""``<root>/mcp.json`` — one entry file, two scopes, one writer.

- ``local.servers`` is the user's own device declarations (stdio, or direct HTTP
  the user holds credentials for). The file is the source of truth; secrets are
  referenced (``credentialRef``), never stored.
- ``managedProfiles[<profile>]`` is a projection of the cloud account's manifest:
  resource refs, execution scope, schema hashes, enable preference. Editing it
  cannot grant anything — the cloud re-authorizes every call.

Writes take a cross-process lock, verify both the on-disk ``generation`` and the actual file digest match the
caller's snapshot, write a temp file and ``os.replace`` it. A corrupt file is never
reset to empty: loading raises ``McpJsonCorrupt`` and the file stays for repair.
Unknown top-level keys are preserved verbatim.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import credentials
from .lockfile import locked
from .paths import mcp_json_path, meta_root, safe_segment

SCHEMA_VERSION = 2
LOCAL_TRANSPORTS = ("stdio", "streamable_http", "sse")

_write_lock = threading.Lock()
logger = logging.getLogger(__name__)


class McpJsonError(RuntimeError):
    pass


class McpJsonCorrupt(McpJsonError):
    def __init__(self, path: Path, cause: Exception) -> None:
        super().__init__(f"{path} is not valid mcp.json: {cause}")
        self.path = path


class McpJsonConflict(McpJsonError):
    pass


@dataclass
class McpJson:
    generation: int = 0
    local: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    managed: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)
    # Digest of the bytes actually read, never a field in the user's JSON.
    # An empty digest represents an absent file.
    digest: str = field(default="", repr=False)

    def to_dict(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "generation": self.generation,
            "local": {"servers": copy.deepcopy(self.local)},
            "managedProfiles": copy.deepcopy(self.managed),
        }
        for key, value in self.extensions.items():
            doc.setdefault(key, value)
        return doc


def _validate_local_server(server_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    safe_segment(server_id)
    if not isinstance(raw, dict):
        raise McpJsonError(f"local server {server_id!r} must be an object")
    transport = str(raw.get("transport") or "")
    if transport not in LOCAL_TRANSPORTS:
        raise McpJsonError(f"local server {server_id!r} has unsupported transport {transport!r}")
    out: Dict[str, Any] = {"transport": transport, "enabled": bool(raw.get("enabled", True))}
    if transport == "stdio":
        command = str(raw.get("command") or "").strip()
        if not command:
            raise McpJsonError(f"local server {server_id!r} needs a command")
        out["command"] = command
        out["args"] = [str(a) for a in (raw.get("args") or [])]
        if raw.get("cwd"):
            out["cwd"] = str(raw["cwd"])
        env = raw.get("env") or {}
        if not isinstance(env, dict):
            raise McpJsonError(f"local server {server_id!r} env must be an object")
        out["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        url = str(raw.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise McpJsonError(f"local server {server_id!r} needs an http(s) url")
        out["url"] = url
    if raw.get("credentialRef"):
        credentials.parse_ref(str(raw["credentialRef"]))
        out["credentialRef"] = str(raw["credentialRef"])
    if "headers" in raw and raw["headers"]:
        raise McpJsonError(
            f"local server {server_id!r} carries inline headers; store them via credentialRef"
        )
    for key in ("displayName", "description", "executionTimeout"):
        if raw.get(key) is not None:
            out[key] = raw[key]
    return out


def _parse(doc: Any, path: Path) -> McpJson:
    if not isinstance(doc, dict):
        raise McpJsonCorrupt(path, ValueError("top level is not an object"))
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        raise McpJsonCorrupt(
            path, ValueError(f"schemaVersion {doc.get('schemaVersion')!r} unsupported")
        )
    gen = doc.get("generation", 0)
    if not isinstance(gen, int) or gen < 0:
        raise McpJsonCorrupt(path, ValueError("generation must be a non-negative integer"))
    local_raw = (doc.get("local") or {}).get("servers") or {}
    managed_raw = doc.get("managedProfiles") or {}
    if not isinstance(local_raw, dict) or not isinstance(managed_raw, dict):
        raise McpJsonCorrupt(path, ValueError("local.servers / managedProfiles must be objects"))
    try:
        local = {sid: _validate_local_server(sid, raw) for sid, raw in local_raw.items()}
    except (McpJsonError, ValueError) as exc:
        raise McpJsonCorrupt(path, exc) from exc
    extensions = {
        k: v
        for k, v in doc.items()
        if k not in ("schemaVersion", "generation", "local", "managedProfiles")
    }
    return McpJson(
        generation=gen, local=local, managed=copy.deepcopy(managed_raw), extensions=extensions
    )


def load(path: Optional[Path] = None) -> McpJson:
    path = path or mcp_json_path()
    if not path.exists():
        return McpJson()
    try:
        raw = path.read_bytes()
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise McpJsonCorrupt(path, exc) from exc
    parsed = _parse(doc, path)
    parsed.digest = hashlib.sha256(raw).hexdigest()
    return parsed


def _lock_path(path: Path) -> Path:
    return meta_root() / f"{path.name}.lock"


def _check_expected(
    doc: McpJson,
    *,
    expected_generation: Optional[int] = None,
    expected_digest: Optional[str] = None,
) -> None:
    if expected_generation is not None and doc.generation != expected_generation:
        raise McpJsonConflict(
            f"mcp.json generation is {doc.generation}, caller expected {expected_generation}"
        )
    if expected_digest is not None and doc.digest != expected_digest:
        raise McpJsonConflict("mcp.json content changed since it was read; reload before saving")


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""


def write(
    doc: McpJson,
    *,
    expected_generation: int,
    expected_digest: Optional[str] = None,
    path: Optional[Path] = None,
) -> McpJson:
    """Commit only if the original file generation and contents still match."""
    path = path or mcp_json_path()
    digest = doc.digest if expected_digest is None else expected_digest
    with _write_lock, locked(_lock_path(path)):
        current = load(path)
        _check_expected(current, expected_generation=expected_generation, expected_digest=digest)
        committed = doc.to_dict()
        committed["generation"] = expected_generation + 1
        payload = json.dumps(committed, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        fd, tmp = tempfile.mkstemp(prefix=".mcp.json.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            # External editors need not honor our lock. Check again after the
            # temporary file is durable so edits during serialization survive.
            if _file_digest(path) != current.digest:
                raise McpJsonConflict("mcp.json content changed while saving; reload before saving")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        doc.generation = expected_generation + 1
        doc.digest = hashlib.sha256(payload).hexdigest()
    return doc


def quarantine_corrupt(path: Optional[Path] = None) -> Optional[Path]:
    """Explicit repair action: move an unreadable file aside (never automatic)."""
    path = path or mcp_json_path()
    try:
        load(path)
        return None
    except McpJsonCorrupt:
        target = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        os.replace(path, target)
        return target


# ── local scope ─────────────────────────────────────────────────────────


def _cleanup_unreferenced_credential(ref: Optional[str]) -> None:
    """Delete only after proving no committed local declaration still uses it."""
    if not ref:
        return
    try:
        path = mcp_json_path()
        with _write_lock, locked(_lock_path(path)):
            current = load(path)
            if any(entry.get("credentialRef") == ref for entry in current.local.values()):
                return
            credentials.delete_secret(ref)
    except Exception as exc:
        # Configuration is already committed (or still references the previous
        # credential). Cleanup failure must not turn that into an ambiguous save.
        # Keep the secret for later cleanup; never include backend error details.
        logger.warning("Credential cleanup deferred for %s (%s)", ref, type(exc).__name__)


def upsert_local_server(
    server_id: str,
    spec: Dict[str, Any],
    *,
    secret_headers: Optional[Dict[str, str]] = None,
    expected_generation: Optional[int] = None,
    expected_digest: Optional[str] = None,
) -> McpJson:
    """Save a local declaration; credential replacement follows JSON commit."""
    doc = load()
    _check_expected(doc, expected_generation=expected_generation, expected_digest=expected_digest)
    previous_ref = (doc.local.get(server_id) or {}).get("credentialRef")
    entry = dict(spec)
    if not entry.get("credentialRef") and previous_ref:
        entry["credentialRef"] = previous_ref
    validated = _validate_local_server(server_id, entry)
    staged_ref = None
    try:
        if secret_headers:
            # A unique staging identity keeps existing callers on their old
            # credential until the JSON reference is successfully replaced.
            staged_ref = credentials.store_headers(
                f"mcp-{server_id}-{uuid.uuid4().hex}", secret_headers
            )
            validated["credentialRef"] = staged_ref
        doc.local[server_id] = validated
        written = write(doc, expected_generation=doc.generation)
    except Exception:
        _cleanup_unreferenced_credential(staged_ref)
        raise
    if previous_ref != validated.get("credentialRef"):
        _cleanup_unreferenced_credential(previous_ref)
    return written


def remove_local_server(
    server_id: str,
    *,
    expected_generation: Optional[int] = None,
    expected_digest: Optional[str] = None,
) -> bool:
    doc = load()
    _check_expected(doc, expected_generation=expected_generation, expected_digest=expected_digest)
    entry = doc.local.pop(server_id, None)
    if entry is None:
        return False
    write(doc, expected_generation=doc.generation)
    _cleanup_unreferenced_credential(entry.get("credentialRef"))
    return True


def local_server_configs(doc: Optional[McpJson] = None) -> Dict[str, dict]:
    """``local.servers`` as assembly config dicts (same shape as DB-row configs)."""
    doc = doc or load()
    out: Dict[str, dict] = {}
    for sid, entry in doc.local.items():
        if not entry.get("enabled", True):
            continue
        cfg: Dict[str, Any] = {
            "transport": entry["transport"],
            "is_stable": False,
            "source_plugin": None,
            "owner_user_id": None,
            "origin": "mcp_json_local",
            "execution_scope": "local",
        }
        if entry["transport"] == "stdio":
            cfg["command"] = entry["command"]
            cfg["args"] = list(entry.get("args") or [])
            if entry.get("cwd"):
                cfg["cwd"] = entry["cwd"]
            if entry.get("env"):
                cfg["env"] = dict(entry["env"])
        else:
            cfg["url"] = entry["url"]
            if entry.get("credentialRef"):
                cfg["headers"] = credentials.load_headers(entry["credentialRef"])
        if entry.get("executionTimeout") is not None:
            cfg["execution_timeout"] = entry["executionTimeout"]
        out[sid] = cfg
    return out


# ── managed scope (cloud projection) ─────────────────────────────────────


def project_managed_profile(
    profile: str,
    *,
    cloud_instance_id: str,
    catalog_revision: str,
    servers: List[Dict[str, Any]],
    enabled_overrides: Optional[Dict[str, bool]] = None,
) -> McpJson:
    """Replace one profile's managed projection from a validated manifest."""
    safe_segment(profile)
    doc = load()
    previous = doc.managed.get(profile) or {}
    prev_servers = previous.get("servers") or {}
    projected: Dict[str, Any] = {}
    for s in servers:
        sid = str(s["server_id"])
        prev = prev_servers.get(sid) or {}
        enabled = bool((enabled_overrides or {}).get(sid, prev.get("enabled", True)))
        projected[sid] = {
            "resourceRef": {
                "issuer": cloud_instance_id,
                "namespace": str(s.get("scope") or "shared"),
                "kind": "mcp",
                "id": sid,
            },
            "component": str(s.get("component") or sid),
            "displayName": str(s.get("display_name") or sid),
            "executionScope": "cloud",
            "gatewayRef": "desktop-capability",
            "schemaHash": str(s.get("schema_hash") or ""),
            "enabled": enabled,
        }
    doc.managed[profile] = {
        "cloudInstanceId": cloud_instance_id,
        "catalogRevision": catalog_revision,
        "servers": projected,
    }
    return write(doc, expected_generation=doc.generation)


def managed_enabled(profile: str, doc: Optional[McpJson] = None) -> Dict[str, bool]:
    doc = doc or load()
    servers = (doc.managed.get(profile) or {}).get("servers") or {}
    return {sid: bool(entry.get("enabled", True)) for sid, entry in servers.items()}


def set_managed_enabled(profile: str, server_id: str, enabled: bool) -> McpJson:
    doc = load()
    entry = ((doc.managed.get(profile) or {}).get("servers") or {}).get(server_id)
    if entry is None:
        raise McpJsonError(f"{server_id!r} is not in profile {profile!r}")
    entry["enabled"] = bool(enabled)
    return write(doc, expected_generation=doc.generation)
