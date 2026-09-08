"""Agent kind on the desktop store: ``R/agents/<profile>/<agent_id>/<revision>/``.

A stored agent is ``agent.json`` (every field but the instructions) plus
``instructions.md``. :class:`AgentDefinition` exposes the same attribute
surface the agent factory reads from a ``UserAgent`` row, so a cloud-defined
agent runs on this device without a database row of its own. Device-created
agents keep their database row as the runtime truth; the store holds a
projection so the directory contract holds for every agent the user can see.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from . import registry, store
from .paths import KIND_AGENT, LOCAL_PROFILE, revision_for_hash
from .ref import local_ref
from .resolver import SOURCE_CLOUD, SOURCE_LOCAL, Candidate, Resolution, resolve

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last: Dict[str, Resolution] = {}

_JSON_FIELDS = (
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
    "extra_config",
    "dependencies",
    "platforms",
    "extensions",
)


@dataclass
class AgentDefinition:
    agent_id: str
    name: str
    system_prompt: str = ""
    owner_type: str = "user"
    user_id: Optional[str] = None
    avatar: Optional[str] = None
    description: str = ""
    welcome_message: str = ""
    suggested_questions: List[str] = field(default_factory=list)
    mcp_server_ids: List[str] = field(default_factory=list)
    skill_ids: List[str] = field(default_factory=list)
    plugin_ids: List[str] = field(default_factory=list)
    kb_ids: List[str] = field(default_factory=list)
    model_provider_id: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_iters: Optional[int] = 10
    timeout: Optional[int] = 120
    is_enabled: bool = True
    sort_order: int = 0
    source_market_slug: Optional[str] = None
    ontology_tags: List[str] = field(default_factory=list)
    version: str = ""
    extra_config: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[Dict[str, Any]] = field(default_factory=list)
    platforms: List[str] = field(default_factory=list)
    extensions: Any = field(default_factory=list)
    created_at: Any = None
    updated_at: Any = None
    created_by: Optional[str] = None
    # Store identity (absent for a plain database projection)
    origin: str = SOURCE_LOCAL
    profile: str = LOCAL_PROFILE
    revision: Optional[str] = None

    @classmethod
    def from_serialized(cls, data: Dict[str, Any]) -> "AgentDefinition":
        kwargs = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        kwargs.setdefault("agent_id", str(data.get("agent_id") or ""))
        kwargs.setdefault("name", str(data.get("name") or ""))
        return cls(**kwargs)

    def to_files(self) -> Dict[str, str]:
        definition = {k: getattr(self, k) for k in _JSON_FIELDS}
        extra = dict(definition.get("extra_config") or {})
        extra.pop("change_history", None)
        definition["extra_config"] = extra
        return {
            "agent.json": json.dumps(definition, ensure_ascii=False, sort_keys=True, indent=2),
            "instructions.md": self.system_prompt or "",
        }

    @classmethod
    def from_dir(cls, path: Path, *, origin: str, profile: str, revision: str) -> "AgentDefinition":
        data = json.loads((path / "agent.json").read_text(encoding="utf-8"))
        data["system_prompt"] = (path / "instructions.md").read_text(encoding="utf-8")
        defn = cls.from_serialized(data)
        defn.origin, defn.profile, defn.revision = origin, profile, revision
        return defn

    def to_serialized(self) -> Dict[str, Any]:
        data = {k: getattr(self, k) for k in _JSON_FIELDS}
        data.update(
            {
                "system_prompt": self.system_prompt,
                "user_id": self.user_id,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "created_by": self.created_by,
                "change_history": [],
                "origin": self.origin,
                "profile": self.profile,
                "revision": self.revision,
            }
        )
        return data

    def content_hash(self) -> str:
        from core.services.desktop_capability_protocol import entity_content_hash

        return entity_content_hash(self.to_files())


# ── store IO ────────────────────────────────────────────────────────────


def load_definition(comp: store.StoredComponent, *, origin: str) -> AgentDefinition:
    return AgentDefinition.from_dir(
        comp.path, origin=origin, profile=comp.profile, revision=comp.revision
    )


def account_definitions(profile: Optional[str] = None) -> List[AgentDefinition]:
    """Ready + enabled agents of the bridged account, loaded from the store."""
    from .skills import current_account_profile

    profile = profile or current_account_profile()
    if not profile:
        return []
    out: List[AgentDefinition] = []
    for inst in registry.list_installations(kind=KIND_AGENT, profile_id=profile):
        if not (inst.ready and inst.enabled):
            continue
        comp = store.get(KIND_AGENT, profile, inst.key, inst.resolved_revision or "")
        if comp is None:
            continue
        try:
            out.append(load_definition(comp, origin=SOURCE_CLOUD))
        except (OSError, ValueError) as exc:
            logger.warning("[caps-agents] unreadable definition %s: %s", comp.path, exc)
    return out


def publish_local_agent(serialized: Dict[str, Any]) -> store.StoredComponent:
    """Project a device database agent into the store (one revision per content)."""
    defn = AgentDefinition.from_serialized(serialized)
    content_hash = defn.content_hash()
    revision = revision_for_hash(content_hash)
    ref = local_ref(KIND_AGENT, defn.agent_id)
    inst = registry.upsert(
        profile_id=LOCAL_PROFILE,
        ref=ref,
        display_name=defn.name,
        description=defn.description,
        version=str(defn.version or ""),
        content_hash=content_hash,
        source=SOURCE_LOCAL,
        payload={"owner_user_id": serialized.get("user_id"), "from_db": True},
        enabled=bool(defn.is_enabled),
    )
    comp = store.get(KIND_AGENT, LOCAL_PROFILE, ref.key, revision)
    if comp is None:
        comp = store.write_from_files(KIND_AGENT, LOCAL_PROFILE, ref.key, revision, defn.to_files())
    if inst.resolved_revision != revision or inst.state != "ready":
        registry.set_state(inst.install_id, "ready", resolved_revision=revision)
    # Keep historical revisions until a reference-aware collector is available.
    return comp


def remove_local_agent(agent_id: str) -> bool:
    from .runtime import references

    if references("agent", LOCAL_PROFILE, agent_id):
        # Source deletion revokes use; history still needs its exact bytes.
        return registry.mark_removed(registry.install_id("agent", LOCAL_PROFILE, agent_id))
    removed = store.remove_key(KIND_AGENT, LOCAL_PROFILE, agent_id) > 0
    return registry.delete(registry.install_id(KIND_AGENT, LOCAL_PROFILE, agent_id)) or removed


# ── resolution by display name (what the user @-mentions) ───────────────


def _candidate(defn: AgentDefinition, *, account_level: bool, install_id: str) -> Candidate:
    return Candidate(
        install_id=install_id,
        runtime_name=defn.name,
        kind=KIND_AGENT,
        profile=defn.profile,
        source=defn.origin,
        path=Path("<agent>"),
        content_hash=defn.content_hash(),
        revision=defn.revision,
        usable=bool(defn.is_enabled),
        account_level=account_level,
        display_name=defn.name,
        description=defn.description,
        version=str(defn.version or ""),
    )


def resolve_visible(user_id: str, local_rows: Iterable[Dict[str, Any]]) -> Resolution:
    """Local database agents + the account's stored agents, one per display name."""
    cands: List[Candidate] = []
    for row in local_rows:
        defn = AgentDefinition.from_serialized(row)
        cands.append(
            _candidate(
                defn,
                account_level=True,
                install_id=registry.install_id(KIND_AGENT, LOCAL_PROFILE, defn.agent_id),
            )
        )
    from .skills import account_authorized_for

    for defn in account_definitions() if account_authorized_for(user_id) else []:
        cands.append(
            _candidate(
                defn,
                account_level=True,
                install_id=registry.install_id(KIND_AGENT, defn.profile, defn.agent_id),
            )
        )
    res = resolve(KIND_AGENT, cands, preferences=registry.preferences(KIND_AGENT, user_id=user_id))
    with _lock:
        _last[user_id] = res
    return res


def last_resolution(user_id: str) -> Optional[Resolution]:
    with _lock:
        return _last.get(user_id)


def merge_visible(user_id: str, local_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The list a device user sees: resolver winners only, cloud ones serialized like rows."""
    res = resolve_visible(user_id, local_rows)
    chosen = {c.install_id for c in res.chosen.values()}
    out: List[Dict[str, Any]] = []
    for row in local_rows:
        if registry.install_id(KIND_AGENT, LOCAL_PROFILE, str(row.get("agent_id"))) in chosen:
            out.append(row)
    for defn in account_definitions():
        if registry.install_id(KIND_AGENT, defn.profile, defn.agent_id) in chosen:
            out.append(defn.to_serialized())
    for name in res.conflicts:
        logger.warning("[caps-agents] agent name conflict, nothing visible for '%s'", name)
    return out


def account_definition(agent_id: str) -> Optional[AgentDefinition]:
    for defn in account_definitions():
        if defn.agent_id == agent_id:
            return defn
    return None
