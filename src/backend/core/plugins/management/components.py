"""Plugin components responsibilities."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from core.agent_skills.binary_files import is_binary_value
from core.agent_skills.deps_detector import detect_dependencies
from core.agent_skills.registry import _load_skill_metadata_from_str
from core.db.models import AdminMcpServer, AdminSkill
from core.infra.exceptions import BadRequestError
from core.infra.time import utc_now
from core.ontology.build_validator import ensure_ontology_build_valid
from core.plugins.packaging import sources as plugin_sources
from core.plugins.packaging.importer import _rewrite_path_vars
from core.plugins.packaging.models import NormalizedSkill
from core.services.marketplace_service import _inject_secrets, _rewrite_frontmatter_name
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


def _merge_tool_metadata(stored: Any, manifest: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep captured tool schemas while refreshing manifest-owned display text.

    ``tools_json`` is the schema of record: probing the running MCP server fills in
    each tool's ``inputSchema``, and the desktop capability manifest ships exactly
    these entries to the model. A plugin manifest only declares ``{name,
    description}``, so installing or upgrading the plugin must not replace the
    stored entries wholesale — that leaves every tool without parameters.
    """
    by_name: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in stored if isinstance(stored, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        by_name[name] = dict(item)
        order.append(name)
    for item in manifest:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        merged = by_name.get(name)
        if merged is None:
            by_name[name] = dict(item)
            order.append(name)
            continue
        for key, value in item.items():
            # Never let a manifest entry blank out a schema captured from the server.
            if key in {"inputSchema", "input_schema"} and not (value or {}):
                continue
            merged[key] = value
    return [by_name[name] for name in order]


def _rewrite_sibling_refs(text: str, sibling_ids: Dict[str, str]) -> str:
    """Rewrite inter-skill relative references ``../<sibling skill name>`` into ``../<sibling skill_id>``.

    In multi-skill plugins (e.g. feishu-cli bundling the 24 official lark
    skills), skills reference each other by sibling directory name — e.g.
    lark-im/SKILL.md writes ``[..](../lark-shared/SKILL.md)``. But after plugin
    installation each skill materializes into its own namespaced directory
    ``/workspace/skills/<slug>-<name>-<fp>/``, so ``../lark-shared/`` would
    point at a nonexistent sibling directory. Here the name segment is replaced
    with that sibling skill's final skill_id (within one installation all
    skills live under /workspace/skills/, still siblings, so ``../<id>/``
    resolves).

    - Keeps the ``../`` depth and the trailing path; only the name segment is
      replaced; multi-level ``../../<name>`` also matches the last segment.
    - The ``(?![\\w-])`` boundary ensures ``lark-vc`` doesn't clobber
      ``lark-vc-agent`` and ``lark-doc`` doesn't touch non-skill references
      like ``../lark-doc-fetch.md`` (those aren't in the mapping, and a
      trailing ``-`` prevents a match).
    - Longest names first, so a prefix name can't steal the match.
    """
    if not text or not sibling_ids:
        return text
    for name in sorted(sibling_ids, key=len, reverse=True):
        text = re.sub(
            r"\.\./" + re.escape(name) + r"(?![\w-])",
            "../" + sibling_ids[name],
            text,
        )
    return text


def _apply_skill(
    db: Session,
    sk: NormalizedSkill,
    *,
    slug: str,
    owner_user_id: Optional[str],
    secrets: Dict[str, str],
    required_secrets: List[Dict[str, Any]],
    enabled: bool,
    validate_ontology_build: bool,
    sibling_ids: Optional[Dict[str, str]] = None,
) -> str:
    """Upsert one normalized skill as an AdminSkill (tagged with source_plugin). Returns the skill_id."""
    skill_id = plugin_sources._make_skill_id(slug, sk.name, owner_user_id)
    sandbox_dir = f"/workspace/skills/{skill_id}"

    # Path-variable rewrite: SKILL.md body + text attachments (binaries untouched)
    content = _rewrite_path_vars(sk.skill_content, skill_sandbox_dir=sandbox_dir)
    extra_files: Dict[str, str] = {}
    for k, v in sk.extra_files.items():
        extra_files[k] = (
            v if is_binary_value(v) else _rewrite_path_vars(str(v), skill_sandbox_dir=sandbox_dir)
        )

    # Inter-skill relative-reference rewrite: ../<sibling skill name> → ../<sibling skill_id> (body + text attachments)
    if sibling_ids:
        content = _rewrite_sibling_refs(content, sibling_ids)
        for k, v in list(extra_files.items()):
            if not is_binary_value(v):
                extra_files[k] = _rewrite_sibling_refs(str(v), sibling_ids)

    # Display name comes from the original frontmatter name (before rewriting into the namespaced id), falling back to the skill directory name.
    try:
        _display_name = _load_skill_metadata_from_str(content, skill_id).name
    except Exception:  # noqa: BLE001
        _display_name = None

    # Rewrite the frontmatter name into the namespaced id (handles both dedup and normalizing illegal names)
    content = _rewrite_frontmatter_name(content, skill_id)

    if required_secrets:
        content = _inject_secrets(content, extra_files, required_secrets, secrets)

    try:
        meta = _load_skill_metadata_from_str(content, skill_id)
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(message=f"技能 {sk.name!r} 的 SKILL.md 不合法：{exc}")

    deps = detect_dependencies({fn: c for fn, c in extra_files.items() if not is_binary_value(c)})
    if validate_ontology_build:
        ensure_ontology_build_valid(
            db,
            asset_type="skill",
            name=_display_name or sk.name or skill_id,
            description=meta.description or "",
            instructions=content,
            tool_names=list(meta.allowed_tools or []),
            ontology_tags=list(meta.tags or []),
        )
    now = utc_now()
    existing = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
    fields = dict(
        skill_content=content,
        display_name=_display_name or sk.name or skill_id,
        description=meta.description or "",
        version=meta.version or "1.0.0",
        tags=list(meta.tags or []),
        allowed_tools=list(meta.allowed_tools or []),
        extra_files=extra_files,
        dependencies=deps,
        is_enabled=enabled,
        owner_user_id=owner_user_id,
        source_plugin=slug,
        updated_at=now,
    )
    if existing is not None:
        for key, val in fields.items():
            setattr(existing, key, val)
        for col in ("tags", "extra_files", "dependencies"):
            flag_modified(existing, col)
    else:
        db.add(AdminSkill(skill_id=skill_id, created_at=now, **fields))
    return skill_id


def _apply_mcp(
    db: Session,
    mc,
    *,
    slug: str,
    owner_user_id: Optional[str],
    enabled: bool,
    validate_ontology_build: bool,
) -> str:
    """Upsert one normalized MCP as an AdminMcpServer (tagged with source_plugin). Returns the server_id.

    stdio (needs_runtime) is force-disabled even when enabled is requested
    (it can only be enabled once the runtime is fully in place).
    """
    server_id = plugin_sources._make_server_id(slug, mc.name, owner_user_id)
    effective_enabled = bool(enabled) and not mc.needs_runtime
    tools_meta = [item for item in list(getattr(mc, "tools", None) or []) if isinstance(item, dict)]
    if validate_ontology_build:
        ensure_ontology_build_valid(
            db,
            asset_type="tool",
            name=mc.display_name or mc.name,
            description=mc.description or (mc.note or ""),
            tool_names=[str(item["name"]) for item in tools_meta if item.get("name")],
            tool_schemas={
                str(item["name"]): item.get("inputSchema")
                or item.get("input_schema")
                or item.get("parameters")
                or {}
                for item in tools_meta
                if item.get("name")
            },
        )
    now = utc_now()
    existing = db.query(AdminMcpServer).filter(AdminMcpServer.server_id == server_id).first()
    fields = dict(
        display_name=mc.display_name,
        description=mc.description or (mc.note or ""),
        transport=mc.transport,
        command=mc.command,
        args=list(mc.args or []),
        url=mc.url,
        env_vars=dict(mc.env_vars or {}),
        headers=dict(mc.headers or {}),
        tools_json=tools_meta,
        is_enabled=effective_enabled,
        owner_user_id=owner_user_id,
        source_plugin=slug,
        updated_at=now,
    )
    if getattr(mc, "cwd", None):
        # stdio working directory (Agent Plugins standard field) — kept for when the runtime lands
        fields["extra_config"] = {"cwd": mc.cwd}
    if existing is not None:
        # A plugin manifest lists its tools for display only ({name, description});
        # the schemas in ``tools_json`` come from probing the running server and are
        # the desktop's only source of tool parameters. Overwriting them with the
        # manifest list would strip every parameter, so merge by name instead:
        # manifest text wins, captured schemas survive.
        fields["tools_json"] = _merge_tool_metadata(existing.tools_json, tools_meta)
        for key, val in fields.items():
            setattr(existing, key, val)
        for col in ("args", "env_vars", "headers", "tools_json"):
            flag_modified(existing, col)
    else:
        db.add(AdminMcpServer(server_id=server_id, created_at=now, **fields))
    return server_id
