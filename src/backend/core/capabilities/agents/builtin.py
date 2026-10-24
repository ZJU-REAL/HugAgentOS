"""Platform-owned default sub-agent definitions.

These profiles are runtime capabilities, not ``UserAgent`` rows.  Keeping them
outside the user-agent table gives the harness stable defaults while leaving
user-created agents editable and removable.
"""

from __future__ import annotations
from core.harness.registry.model_services import render_prompt_part

import json
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional

from extensions.paths import DEFAULT_AGENTS_DIR

logger = logging.getLogger(__name__)

BUILTIN_AGENT_PREFIX = "builtin."

@dataclass(frozen=True)
class BuiltinSubagentSpec:
    role: str
    name: str
    description: str
    shared_context: bool
    max_iters: int
    sort_order: int = 0

    @property
    def agent_id(self) -> str:
        return f"{BUILTIN_AGENT_PREFIX}{self.role}"


def _load_builtin_subagents() -> tuple[BuiltinSubagentSpec, ...]:
    specs = []
    for manifest in sorted(DEFAULT_AGENTS_DIR.glob("*/agent.json")):
        spec = BuiltinSubagentSpec(**json.loads(manifest.read_text(encoding="utf-8")))
        if spec.role != manifest.parent.name:
            raise ValueError(f"builtin agent role does not match its directory: {manifest}")
        specs.append(spec)
    if not specs or len({spec.agent_id for spec in specs}) != len(specs):
        raise ValueError("builtin agent definitions are missing or duplicated")
    return tuple(sorted(specs, key=lambda spec: spec.sort_order))


BUILTIN_SUBAGENTS = _load_builtin_subagents()

_BUILTIN_BY_ID = {spec.agent_id: spec for spec in BUILTIN_SUBAGENTS}


def get_builtin_subagent(agent_id: str) -> Optional[BuiltinSubagentSpec]:
    return _BUILTIN_BY_ID.get(str(agent_id or ""))


def effective_builtin_capabilities(
    spec: BuiltinSubagentSpec,
    parent_runtime: Optional[Mapping[str, Any]] = None,
) -> Dict[str, List[str]]:
    """Return the parent's effective capabilities; built-ins never add grants.

    Role boundaries (e.g. the explorer and reviewer not writing) are expressed
    in each role's prompt, not by narrowing the tool surface.
    """
    runtime = parent_runtime or {}
    parent_mcp = runtime.get("enabled_mcp_ids")
    parent_skills = runtime.get("enabled_skill_ids")
    parent_kbs = runtime.get("enabled_kb_ids")
    mcp_ids = list(parent_mcp) if isinstance(parent_mcp, list) else []
    skill_ids = list(parent_skills) if isinstance(parent_skills, list) else []
    kb_ids = list(parent_kbs) if isinstance(parent_kbs, list) else []

    return {
        "mcp_server_ids": mcp_ids,
        "skill_ids": skill_ids,
        "kb_ids": kb_ids,
    }


def list_builtin_subagents(
    parent_runtime: Optional[Mapping[str, Any]] = None,
    *,
    disabled_agent_ids: Iterable[str] = (),
    include_prompt: bool = False,
) -> List[Dict[str, Any]]:
    """Serialize platform defaults into the existing visible-agent shape.

    Runtime callers normally omit ``include_prompt`` because the router needs
    only the capability summary.  The user-facing agent library enables it so
    the detail page can show the role prompt without duplicating prompt-loading
    logic in the API layer.
    """
    disabled_ids = {str(agent_id) for agent_id in disabled_agent_ids}
    items: List[Dict[str, Any]] = []
    for sort_order, spec in enumerate(BUILTIN_SUBAGENTS):
        capabilities = effective_builtin_capabilities(spec, parent_runtime)
        item = {
            "agent_id": spec.agent_id,
            "name": spec.name,
            "avatar": None,
            "description": spec.description,
            "system_prompt": load_builtin_subagent_prompt(spec) if include_prompt else "",
            "welcome_message": "",
            "suggested_questions": [],
            "is_enabled": spec.agent_id not in disabled_ids,
            "owner_type": "builtin",
            "user_id": None,
            "mcp_server_ids": capabilities["mcp_server_ids"],
            "skill_ids": capabilities["skill_ids"],
            "kb_ids": capabilities["kb_ids"],
            "plugin_ids": [],
            "model_provider_id": None,
            "temperature": None,
            "max_tokens": None,
            "max_iters": spec.max_iters,
            "timeout": 120,
            "sort_order": sort_order,
            "ontology_tags": [],
            "version": "builtin",
            "change_history": [],
            "created_at": None,
            "updated_at": None,
            "created_by": None,
            "extra_config": {
                "builtin_role": spec.role,
                "shared_context": spec.shared_context,
                "context_policy": (
                    "inherit_parent" if spec.shared_context else "independent_brief"
                ),
                "sandbox_tools_enabled": bool(
                    (parent_runtime or {}).get("sandbox_tools_enabled", False)
                ),
                "code_capability_enabled": bool(
                    (parent_runtime or {}).get("code_capability_enabled", False)
                ),
            },
        }
        items.append(item)
    return items


def merge_builtin_subagents(
    user_agents: Optional[List[Dict[str, Any]]],
    parent_runtime: Optional[Mapping[str, Any]] = None,
    *,
    disabled_agent_ids: Iterable[str] = (),
    include_disabled: bool = False,
    include_prompt: bool = False,
) -> List[Dict[str, Any]]:
    """Prepend enabled built-ins and preserve all non-conflicting user agents.

    ``include_disabled`` is used by management/library views and by callers
    building an explicit-invocation candidate set. Normal runtime assembly
    leaves it false; explicit callers must narrow the result to the selected
    role before exposing it to the model.
    """
    builtins = list_builtin_subagents(
        parent_runtime,
        disabled_agent_ids=disabled_agent_ids,
        include_prompt=include_prompt,
    )
    if not include_disabled:
        builtins = [item for item in builtins if item["is_enabled"]]
    # Keep every reserved platform ID protected even when that role is disabled
    # and therefore omitted from the runtime-visible list.
    builtin_ids = set(_BUILTIN_BY_ID)
    return builtins + [
        item for item in (user_agents or []) if str(item.get("agent_id") or "") not in builtin_ids
    ]


def refresh_builtin_subagents(
    visible_agents: Optional[List[Dict[str, Any]]],
    parent_runtime: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Refresh only built-ins already present using the parent's final grants.

    The workflow assembles the visible-agent catalog before ``agent_factory``
    finishes resolving catalog defaults, skill-bound MCPs, permission filters,
    and per-run switches. Replacing the existing built-in rows at that later
    point keeps both the routing prompt and child execution on the same
    least-privilege capability snapshot without adding built-ins to callers
    that intentionally supplied only custom agents.
    """
    refreshed = {item["agent_id"]: item for item in list_builtin_subagents(parent_runtime)}
    return [refreshed.get(str(item.get("agent_id") or ""), item) for item in (visible_agents or [])]


def load_builtin_subagent_prompt(spec: BuiltinSubagentSpec) -> str:
    """Load the active DB prompt part, falling back to the versioned file."""
    try:

        prompt = render_prompt_part("subagents", spec.role)
        if prompt:
            return prompt
    except Exception:
        logger.debug("failed to load active builtin subagent prompt", exc_info=True)

    path = DEFAULT_AGENTS_DIR / spec.role / "system.md"
    if not path.is_file():
        raise RuntimeError(f"missing builtin subagent prompt: {path}")
    return path.read_text(encoding="utf-8").strip()


def build_builtin_runtime_profile(
    spec: BuiltinSubagentSpec,
    parent_runtime: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Build the attribute-compatible profile consumed by agent_factory."""
    capabilities = effective_builtin_capabilities(spec, parent_runtime)
    return SimpleNamespace(
        agent_id=spec.agent_id,
        name=spec.name,
        system_prompt=load_builtin_subagent_prompt(spec),
        mcp_server_ids=capabilities["mcp_server_ids"],
        skill_ids=capabilities["skill_ids"],
        kb_ids=capabilities["kb_ids"],
        plugin_ids=[],
        model_provider_id=None,
        temperature=None,
        max_tokens=None,
        timeout=None,
        max_iters=spec.max_iters,
        extra_config={
            "builtin_role": spec.role,
            "shared_context": spec.shared_context,
            "context_policy": ("inherit_parent" if spec.shared_context else "independent_brief"),
        },
    )
