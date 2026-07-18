"""Plugin lifecycle service: install / import / uninstall / toggle / list.

A plugin = an installable/removable unit bundling "skills + MCP (+ prompts)".
This service takes a ``NormalizedPlugin`` (read by ``plugin_importer`` from a
native / Claude Code / Codex package) and splits it into the existing
subsystems:

- skills → ``AdminSkill`` (reusing marketplace's flattening / namespacing /
  secret injection / dependency detection logic), tagged with ``source_plugin``
- MCP → ``AdminMcpServer``, tagged with ``source_plugin``; stdio
  (needs_runtime) is disabled by default
- one ``InstalledPlugin`` record (component_ids + import_report), used by
  uninstall for precise reverse deletion

**Zero runtime changes**: owned private skills / MCP with ``is_enabled=True``
are automatically merged into the available set by
``resolve_all_runtime_enabled``'s ``_owned_enabled_ids``.

See internal design docs for details.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.agent_skills.binary_files import is_binary_value
from core.agent_skills.cache_refresh import refresh_skill_caches
from core.agent_skills.deps_detector import detect_dependencies
from core.agent_skills.registry import _load_skill_metadata_from_str
from core.config.catalog_resolver import invalidate_capability_cache
from core.db.models import (
    AdminMcpServer,
    AdminSkill,
    InstalledPlugin,
    PluginMarketPackage,
    PluginMarketSkillExclusion,
)
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services.marketplace_service import (
    _inject_secrets,
    _rewrite_frontmatter_name,
    _strip_frontmatter,
    compute_install_id,
)
from core.services.plugin_importer import (
    NormalizedPlugin,
    NormalizedSkill,
    _rewrite_path_vars,
    normalize_plugin_dir,
)

logger = logging.getLogger(__name__)

# Plugin bundle root: src/backend/plugin_bundles/{default,marketplace}/<slug>/
# Structure mirrors skill_bundles — default = core plugins shipped with the
# product, marketplace = installable plugin marketplace. Both subdirectories
# are scanned as "plugin marketplace" sources; slug resolution searches across
# both.
PLUGIN_BUNDLES_DIR = Path(__file__).resolve().parents[2] / "plugin_bundles"
PLUGIN_SOURCE_DIRS = (PLUGIN_BUNDLES_DIR / "default", PLUGIN_BUNDLES_DIR / "marketplace")

MAX_ZIP_BYTES = 50 * 1024 * 1024  # pre-extraction cap for uploaded/imported plugin zips


def _iter_plugin_dirs():
    """Iterate over all plugin bundle directories containing plugin.json under default + marketplace."""
    for root in PLUGIN_SOURCE_DIRS:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "plugin.json").is_file():
                yield child


def _resolve_plugin_dir(slug: str) -> Optional[Path]:
    """Locate a plugin bundle directory by slug in default / marketplace (default takes precedence)."""
    if not slug or "/" in slug or ".." in slug:
        return None
    for root in PLUGIN_SOURCE_DIRS:
        d = root / slug
        if d.is_dir() and (d / "plugin.json").is_file():
            return d
    return None


# ── id generation (namespaced) ───────────────────────────────────────────────

def _make_plugin_install_id(slug: str, owner_user_id: Optional[str]) -> str:
    return f"{slug}@{owner_user_id or 'global'}"


def _sanitize_id(value: str, maxlen: int) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (value or "").lower()).strip("-")
    return (s or "x")[:maxlen]


def _make_skill_id(slug: str, skill_name: str, owner_user_id: Optional[str]) -> str:
    """Namespaced skill id: {slug}-{skill} (+ user fingerprint). Constrained by _ID_RE (<=63)."""
    base = _sanitize_id(f"{slug}-{skill_name}", 50)
    return compute_install_id(base, owner_user_id)  # appends -<6-char fingerprint> when owner is non-empty


def _make_server_id(slug: str, server_name: str, owner_user_id: Optional[str]) -> str:
    """Namespaced MCP server id: {slug}-{server} (+ user fingerprint), same rules as skill ids."""
    return compute_install_id(_sanitize_id(f"{slug}-{server_name}", 80), owner_user_id)


# ── Rewriting inter-skill relative references ────────────────────────────────

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


# ── Persistence: single component ────────────────────────────────────────────

def _apply_skill(
    db: Session,
    sk: NormalizedSkill,
    *,
    slug: str,
    owner_user_id: Optional[str],
    secrets: Dict[str, str],
    required_secrets: List[Dict[str, Any]],
    enabled: bool,
    sibling_ids: Optional[Dict[str, str]] = None,
) -> str:
    """Upsert one normalized skill as an AdminSkill (tagged with source_plugin). Returns the skill_id."""
    skill_id = _make_skill_id(slug, sk.name, owner_user_id)
    sandbox_dir = f"/workspace/skills/{skill_id}"

    # Path-variable rewrite: SKILL.md body + text attachments (binaries untouched)
    content = _rewrite_path_vars(sk.skill_content, skill_sandbox_dir=sandbox_dir)
    extra_files: Dict[str, str] = {}
    for k, v in sk.extra_files.items():
        extra_files[k] = v if is_binary_value(v) else _rewrite_path_vars(str(v), skill_sandbox_dir=sandbox_dir)

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

    deps = detect_dependencies(
        {fn: c for fn, c in extra_files.items() if not is_binary_value(c)}
    )
    now = datetime.utcnow()
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
) -> str:
    """Upsert one normalized MCP as an AdminMcpServer (tagged with source_plugin). Returns the server_id.

    stdio (needs_runtime) is force-disabled even when enabled is requested
    (it can only be enabled once the runtime is fully in place).
    """
    server_id = _make_server_id(slug, mc.name, owner_user_id)
    effective_enabled = bool(enabled) and not mc.needs_runtime
    now = datetime.utcnow()
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
        tools_json=list(getattr(mc, "tools", None) or []),
        is_enabled=effective_enabled,
        owner_user_id=owner_user_id,
        source_plugin=slug,
        updated_at=now,
    )
    if existing is not None:
        for key, val in fields.items():
            setattr(existing, key, val)
        for col in ("args", "env_vars", "headers", "tools_json"):
            flag_modified(existing, col)
    else:
        db.add(AdminMcpServer(server_id=server_id, created_at=now, **fields))
    return server_id


# ── Persistence: whole plugin ────────────────────────────────────────────────

def _apply_normalized(
    db: Session,
    np: NormalizedPlugin,
    *,
    owner_user_id: Optional[str],
    secrets: Dict[str, str],
    source: str,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a NormalizedPlugin and return the installation result (including the import_report)."""
    install_id = _make_plugin_install_id(np.slug, owner_user_id)

    de_skills = set(np.default_enabled.get("skills") or [])
    de_mcp = set(np.default_enabled.get("mcp") or [])

    # Skills the admin removed from the marketplace: excluded at install time (applies uniformly to builtin/uploaded packages)
    excluded = get_market_skill_exclusions(db, np.slug)
    install_skills = [sk for sk in np.skills if sk.name not in excluded]

    imported: List[Dict[str, str]] = []
    adapted: List[Dict[str, str]] = []
    skill_ids: List[str] = []
    server_ids: List[str] = []

    # Sibling skill name → final skill_id mapping (used to rewrite inter-skill ../<name> relative references)
    sibling_ids = {
        sk.name: _make_skill_id(np.slug, sk.name, owner_user_id) for sk in install_skills
    }

    for sk in install_skills:
        sid = _apply_skill(
            db, sk, slug=np.slug, owner_user_id=owner_user_id,
            secrets=secrets, required_secrets=np.required_secrets,
            enabled=(sk.name in de_skills) or not de_skills,
            sibling_ids=sibling_ids,
        )
        skill_ids.append(sid)
        imported.append({"type": "skill", "id": sid, "name": sk.name})

    for mc in np.mcp:
        sid = _apply_mcp(
            db, mc, slug=np.slug, owner_user_id=owner_user_id,
            enabled=(mc.name in de_mcp),
        )
        server_ids.append(sid)
        if mc.needs_runtime:
            adapted.append({"type": "mcp", "id": sid, "name": mc.name,
                            "note": mc.note or "stdio MCP 已装上但禁用，需运行时"})
        else:
            imported.append({"type": "mcp", "id": sid, "name": mc.name})

    import_report = {"imported": imported, "adapted": adapted, "dropped": np.dropped}
    component_ids = {"skills": skill_ids, "mcp": server_ids, "prompts": []}

    now = datetime.utcnow()
    existing = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    fields = dict(
        slug=np.slug, name=np.name, version=np.version, description=np.description,
        category=np.category, icon=np.icon, owner_user_id=owner_user_id,
        source=source, component_ids=component_ids, import_report=import_report,
        updated_at=now,
    )
    if existing is not None:
        for key, val in fields.items():
            setattr(existing, key, val)
        for col in ("component_ids", "import_report"):
            flag_modified(existing, col)
        action = "updated"
    else:
        db.add(InstalledPlugin(install_id=install_id, created_at=now, created_by=created_by, **fields))
        action = "installed"

    db.commit()
    _refresh_after_change(owner_user_id)
    logger.info(
        "plugin_%s: slug=%s kind=%s owner=%s skills=%d mcp=%d dropped=%d",
        action, np.slug, np.kind, owner_user_id or "global",
        len(skill_ids), len(server_ids), len(np.dropped),
    )
    return {
        "install_id": install_id,
        "slug": np.slug,
        "name": np.name,
        "kind": np.kind,
        "action": action,
        "import_report": import_report,
    }


def _refresh_after_change(owner_user_id: Optional[str]) -> None:
    """Invalidate related caches after install/uninstall: skill cache + capability-resolution cache + MCP config cache."""
    try:
        refresh_skill_caches()
    except Exception as exc:  # noqa: BLE001
        logger.debug("refresh_skill_caches failed: %s", exc)
    try:
        invalidate_capability_cache(owner_user_id)
        # The 30s capability cache for owned items is keyed by user_id; a global plugin affects all users → clear everything
        if owner_user_id is None:
            invalidate_capability_cache(None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("invalidate_capability_cache failed: %s", exc)
    try:
        from core.services.mcp_service import McpServerConfigService
        McpServerConfigService.get_instance().invalidate_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug("mcp cache invalidate failed: %s", exc)


def builtin_plugin_component_ids() -> Tuple[set, set]:
    """Component ids declared in plugin.json by builtin plugin bundles (``plugin_bundles/{default,marketplace}``).

    Returns ``(skill_ids, mcp_ids)`` — the union of all plugin manifests'
    ``components.skills`` / ``components.mcp``. Even when **not installed**,
    these components already bubble up as first-class entries via static paths
    (e.g. MCP via ``_ports.py`` → catalog.json), so this is used to remove them
    from the "skill library / MCP tool library" and show them only under
    "Plugins", complementing the DB installation source
    (``AdminMcpServer.source_plugin`` non-empty). Pure filesystem scan, no DB
    dependency.
    """
    import json

    skill_ids: set = set()
    mcp_ids: set = set()
    for child in _iter_plugin_dirs():
        try:
            m = json.loads((child / "plugin.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        comps = m.get("components") if isinstance(m, dict) else None
        if not isinstance(comps, dict):
            continue
        for sid in comps.get("skills") or []:
            if isinstance(sid, str) and sid:
                skill_ids.add(sid)
        for mid in comps.get("mcp") or []:
            if isinstance(mid, str) and mid:
                mcp_ids.add(mid)
    return skill_ids, mcp_ids


# ── Public API ────────────────────────────────────────────────────────────────

def _scan_native_manifest(plugin_dir: Path) -> Optional[Dict[str, Any]]:
    """Lightweight read of a builtin plugin bundle's display metadata (no full normalize)."""
    import json
    mp = plugin_dir / "plugin.json"
    if not mp.is_file():
        return None
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(m, dict) or not m.get("name"):
        return None
    skills_root = plugin_dir / "skills"
    skills_count = (
        sum(1 for c in skills_root.iterdir() if c.is_dir() and (c / "SKILL.md").is_file())
        if skills_root.is_dir() else 0
    )
    return {
        "slug": _sanitize_id(str(m.get("name")), 100),
        "name": str(m.get("display_name") or m.get("name")),
        "version": str(m.get("version") or "1.0.0"),
        "description": str(m.get("description") or ""),
        "category": str(m.get("category") or ""),
        "icon": m.get("icon"),
        "skills_count": skills_count,
        "required_secrets": list(m.get("required_secrets") or []),
        "has_admin_config": isinstance(m.get("admin_config"), dict)
        and bool((m.get("admin_config") or {}).get("fields")),
    }


def list_plugins(
    db: Session, owner_user_id: Optional[str], *, include_disabled: bool = False
) -> List[Dict[str, Any]]:
    """Plugin marketplace list (scans plugin_bundles/{default,marketplace}, annotated with installed status).

    ``include_disabled``: the admin panel passes True (sees everything +
    ``market_enabled`` annotation); the user side defaults to False (sees only
    published entries).
    """
    items: List[Dict[str, Any]] = []
    seen_slugs: set = set()
    for child in _iter_plugin_dirs():
        meta = _scan_native_manifest(child)
        if meta:
            meta["source"] = "builtin"
            items.append(meta)
            seen_slugs.add(meta["slug"])
    # DB marketplace packages (admin-uploaded and published): skipped when the filesystem already has the same slug
    for row in db.query(PluginMarketPackage).order_by(PluginMarketPackage.created_at.desc()).all():
        if row.slug in seen_slugs:
            continue
        items.append(_market_meta_dict(row))
        seen_slugs.add(row.slug)
    # Subtract skills the admin removed from the marketplace (aggregated once per slug) so skills_count reflects the real offering
    excl_by_slug: Dict[str, set] = {}
    for ex_slug, ex_name in db.query(
        PluginMarketSkillExclusion.slug, PluginMarketSkillExclusion.skill_name
    ).all():
        excl_by_slug.setdefault(ex_slug, set()).add(ex_name)
    if excl_by_slug:
        for it in items:
            n_excl = len(excl_by_slug.get(it["slug"], ()))
            if n_excl:
                it["skills_count"] = max(0, int(it.get("skills_count") or 0) - n_excl)

    # Annotate installed status
    if items:
        install_ids = {it["slug"]: _make_plugin_install_id(it["slug"], owner_user_id) for it in items}
        present = {
            row[0] for row in db.query(InstalledPlugin.install_id).filter(
                InstalledPlugin.install_id.in_(list(install_ids.values()))
            ).all()
        }
        for it in items:
            it["installed"] = install_ids[it["slug"]] in present

    # Marketplace publish toggle + visibility scope: users see only published
    # entries visible to them; the admin panel sees everything with
    # annotations. For user-side callers owner_user_id is the current browsing
    # user, reused directly as the viewer.
    from core.services import marketplace_listing as ml
    items = ml.annotate_and_filter(
        db, ml.KIND_PLUGIN, items, id_key="slug",
        include_disabled=include_disabled, viewer_user_id=owner_user_id,
    )
    return items


def set_plugin_market_enabled(
    db: Session, slug: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    """Publish/unpublish a marketplace plugin (controls display in the plugin marketplace; does not affect installed instances)."""
    from core.services import marketplace_listing as ml
    res = ml.set_listing_enabled(db, ml.KIND_PLUGIN, slug, enabled, updated_by=updated_by)
    logger.info("plugin_market_listing: slug=%s enabled=%s", slug, enabled)
    return res


def list_installed(
    db: Session, owner_user_id: Optional[str], *, include_global: bool = False
) -> List[Dict[str, Any]]:
    """Installed plugins, with an aggregated enabled flag (enabled if any component is enabled).

    - owner_user_id=None: global plugins only (admin view).
    - owner_user_id=<user> + include_global=False: that user's private ones only.
    - owner_user_id=<user> + include_global=True: user private + admin global
      plugins (frontend user view; global items are read-only — consistent
      with the skill library showing global skills).
    """
    from sqlalchemy import or_

    q = db.query(InstalledPlugin)
    if owner_user_id is None:
        q = q.filter(InstalledPlugin.owner_user_id.is_(None))
    elif include_global:
        q = q.filter(or_(
            InstalledPlugin.owner_user_id == owner_user_id,
            InstalledPlugin.owner_user_id.is_(None),
        ))
    else:
        q = q.filter(InstalledPlugin.owner_user_id == owner_user_id)
    rows = q.order_by(InstalledPlugin.created_at.desc()).all()
    # Dedup: in the user view (include_global), if the same plugin (slug) was
    # both installed globally by an admin and privately by the user, keep only
    # the admin global version (global read-only takes precedence) — otherwise
    # the plugin library would show two same-named plugins (global
    # install_id=slug@global, private slug@<uid>; the ids differ so both made
    # the list).
    if owner_user_id is not None and include_global:
        global_slugs = {r.slug for r in rows if r.owner_user_id is None}
        rows = [
            r for r in rows
            if r.owner_user_id is None or r.slug not in global_slugs
        ]
    # Enabled state:
    # - user view (owner_user_id non-empty): determined by the user's
    #   "effectively enabled" set (including per-user overrides), so the user's
    #   personal toggles on global plugins are reflected correctly.
    # - admin view (owner_user_id empty): determined by components' global is_enabled.
    if owner_user_id is not None:
        from core.config.catalog_resolver import resolve_all_runtime_enabled
        eff_skills, _eff_agents, eff_mcps = resolve_all_runtime_enabled(db, owner_user_id)
        enabled_skills = set(eff_skills or [])
        enabled_mcps = set(eff_mcps or [])
    else:
        all_skill_ids: set = set()
        all_mcp_ids: set = set()
        for r in rows:
            cids = r.component_ids or {}
            all_skill_ids.update(cids.get("skills") or [])
            all_mcp_ids.update(cids.get("mcp") or [])
        enabled_skills = {
            row[0] for row in db.query(AdminSkill.skill_id).filter(
                AdminSkill.skill_id.in_(all_skill_ids), AdminSkill.is_enabled.is_(True)
            ).all()
        } if all_skill_ids else set()
        enabled_mcps = {
            row[0] for row in db.query(AdminMcpServer.server_id).filter(
                AdminMcpServer.server_id.in_(all_mcp_ids), AdminMcpServer.is_enabled.is_(True)
            ).all()
        } if all_mcp_ids else set()

    out: List[Dict[str, Any]] = []
    for r in rows:
        cids = r.component_ids or {}
        enabled = any(s in enabled_skills for s in (cids.get("skills") or [])) or \
                  any(m in enabled_mcps for m in (cids.get("mcp") or []))
        out.append(_installed_to_dict(r, enabled=enabled))
    return out


def _installed_to_dict(r: InstalledPlugin, *, enabled: bool = True) -> Dict[str, Any]:
    cids = r.component_ids or {}
    return {
        "install_id": r.install_id,
        "slug": r.slug,
        "name": r.name,
        "is_global": r.owner_user_id is None,  # admin global install (read-only on the frontend)
        "version": r.version,
        "description": r.description or "",
        "category": r.category or "",
        "icon": r.icon,
        "source": r.source,
        "enabled": enabled,
        "skills": cids.get("skills") or [],
        "mcp": cids.get("mcp") or [],
        "import_report": r.import_report or {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "has_admin_config": _has_admin_config_for_slug(r.slug),
    }


def get_installed_detail(
    db: Session, install_id: str, *, owner_user_id: Optional[str]
) -> Dict[str, Any]:
    """Full detail of an installed plugin: skills (with instructions/file list) + MCP (with tool list).

    Powers the frontend's three-level drill-in: "open plugin → view components
    → open a single skill/MCP for details".
    """
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    # Details are viewable for one's own private plugins, or admin global plugins (empty owner, viewable by everyone).
    if row.owner_user_id is not None and row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权查看该插件")

    cids = row.component_ids or {}
    skill_ids = cids.get("skills") or []
    server_ids = cids.get("mcp") or []

    # Component enabled state: user view uses their "effectively enabled" set (including per-user overrides); admin view uses is_enabled.
    eff_skills: Optional[set] = None
    eff_mcps: Optional[set] = None
    if owner_user_id is not None:
        from core.config.catalog_resolver import resolve_all_runtime_enabled
        es, _ea, em = resolve_all_runtime_enabled(db, owner_user_id)
        eff_skills, eff_mcps = set(es or []), set(em or [])

    skills_out: List[Dict[str, Any]] = []
    if skill_ids:
        for s in db.query(AdminSkill).filter(AdminSkill.skill_id.in_(skill_ids)).all():
            extra = s.extra_files or {}
            # secrets.json is not shown as an ordinary file
            files = sorted(k for k in extra.keys() if k != "secrets.json")
            sk_enabled = (s.skill_id in eff_skills) if eff_skills is not None else bool(s.is_enabled)
            skills_out.append({
                "skill_id": s.skill_id,
                "name": s.display_name or s.skill_id,
                "description": s.description or "",
                "version": s.version or "",
                "tags": list(s.tags or []),
                "enabled": sk_enabled,
                "instructions": _strip_frontmatter(s.skill_content),
                "files": files,
                "has_secrets": "secrets.json" in extra,
            })

    mcp_out: List[Dict[str, Any]] = []
    if server_ids:
        for m in db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(server_ids)).all():
            raw_tools = m.tools_json or []
            tools = [
                {"name": str(tdef.get("name") or ""), "description": str(tdef.get("description") or "")}
                for tdef in raw_tools if isinstance(tdef, dict)
            ]
            mc_enabled = (m.server_id in eff_mcps) if eff_mcps is not None else bool(m.is_enabled)
            mcp_out.append({
                "server_id": m.server_id,
                "name": m.display_name or m.server_id,
                "description": m.description or "",
                "transport": m.transport,
                "url": m.url,
                "enabled": mc_enabled,
                "needs_runtime": m.transport == "stdio",
                "tools": tools,
            })

    return {
        "install_id": row.install_id,
        "slug": row.slug,
        "name": row.name,
        "is_global": row.owner_user_id is None,
        "version": row.version,
        "description": row.description or "",
        "category": row.category or "",
        "icon": row.icon,
        "source": row.source,
        "import_report": row.import_report or {},
        "skills": skills_out,
        "mcp": mcp_out,
        # Current admin-level config state (user side is read-only: returns only whether each field is set + overall readiness, never real values)
        "admin_config": _admin_config_view(_admin_config_for_slug(row.slug), with_values=False),
        # Account connection type (dingtalk / lark / None): the frontend uses this to render the account-connection panel on the detail page
        "connection": _connection_for_slug(row.slug),
    }


# ── Admin-level config (admin_config): provider credentials, configured by the admin on the plugin detail page, stored in SystemConfig ──
# Shared by all users, read-only on the user side. Storage reuses SystemConfig
# (keys already registered by each service's seed); writes are allowed only for
# field keys the plugin declared (prevents privilege escalation into writing
# arbitrary system config).

def _admin_config_for_slug(slug: str) -> Optional[Dict[str, Any]]:
    """Locate the plugin bundle by slug and read its admin_config declaration; None if absent."""
    plugin_dir = _resolve_plugin_dir(slug)
    if plugin_dir is None:
        return None
    try:
        return normalize_plugin_dir(plugin_dir).admin_config
    except Exception:  # noqa: BLE001
        return None


def _connection_for_slug(slug: str) -> Optional[str]:
    """Read a plugin's account connection type by slug (e.g. dingtalk / lark); None if absent.

    Like admin_config, read live from the bundle at detail-view time, not
    stored in a DB column — imported plugins (whose bundle no longer exists)
    naturally return None without affecting display.
    """
    plugin_dir = _resolve_plugin_dir(slug)
    if plugin_dir is None:
        return None
    try:
        import json
        m = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        conn = m.get("connection")
        return str(conn).strip() if conn else None
    except Exception:  # noqa: BLE001
        return None


def _has_admin_config_for_slug(slug: str) -> bool:
    """Lightweight check for whether a plugin declares admin_config (reads only plugin.json, no full normalize)."""
    plugin_dir = _resolve_plugin_dir(slug)
    if plugin_dir is None:
        return False
    try:
        import json
        m = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        ac = m.get("admin_config")
        return isinstance(ac, dict) and bool(ac.get("fields"))
    except Exception:  # noqa: BLE001
        return False


def _is_set(val: Optional[str]) -> bool:
    return bool((val or "").strip())


def _admin_config_configured(mode: str, sets: List[bool]) -> bool:
    """mode=any → ready if any field is set; mode=all → all fields must be set."""
    if not sets:
        return False
    return any(sets) if mode == "any" else all(sets)


def _admin_config_view(admin_config: Optional[Dict[str, Any]], *, with_values: bool) -> Optional[Dict[str, Any]]:
    """Compute the current admin_config state.

    with_values=False (user side, read-only): returns only is_set +
    configured, **never real values**.
    with_values=True (admin editing): includes current values — a set secret
    returns the mask ``****``, an unset one returns empty; non-secrets return
    real values for the admin to view/edit.
    """
    if not admin_config:
        return None
    from core.services.system_config import SystemConfigService

    svc = SystemConfigService.get_instance()
    fields: List[Dict[str, Any]] = []
    sets: List[bool] = []
    for f in admin_config.get("fields") or []:
        raw = svc.get(f["key"])
        s = _is_set(raw)
        sets.append(s)
        item = {"key": f["key"], "label": f["label"], "secret": f["secret"],
                "description": f["description"], "is_set": s}
        if with_values:
            item["value"] = (("****" if s else "") if f["secret"] else (raw or ""))
        fields.append(item)
    mode = admin_config.get("mode") or "all"
    return {
        "mode": mode,
        "group": admin_config.get("group") or "",
        "hint": admin_config.get("hint") or "",
        "configured": _admin_config_configured(mode, sets),
        "fields": fields,
    }


def get_plugin_admin_config(slug: str) -> Dict[str, Any]:
    """Admin side: get a plugin's admin config (schema + current values, secrets masked)."""
    ac = _admin_config_for_slug(slug)
    if not ac:
        raise ResourceNotFoundError("plugin_admin_config", slug)
    return _admin_config_view(ac, with_values=True)


def set_plugin_admin_config(
    slug: str, values: Dict[str, str], *, updated_by: str = "admin"
) -> Dict[str, Any]:
    """Admin side: write a plugin's admin config. Only field keys the plugin
    declared may be written (prevents privilege escalation into writing
    arbitrary SystemConfig); masked secret values (containing ``****``) are
    skipped by bulk_set and never overwrite the real value."""
    ac = _admin_config_for_slug(slug)
    if not ac:
        raise ResourceNotFoundError("plugin_admin_config", slug)
    allowed = {f["key"] for f in (ac.get("fields") or [])}
    items = [{"key": str(k), "value": v} for k, v in (values or {}).items() if k in allowed]
    if not items:
        raise BadRequestError(message="没有可写入的配置项（key 不属于该插件）")
    from core.services.system_config import SystemConfigService

    SystemConfigService.get_instance().bulk_set(items, updated_by=updated_by)
    return _admin_config_view(ac, with_values=True)


def _normalize_market_plugin(slug: str, db: Optional[Session]) -> NormalizedPlugin:
    """Fetch and normalize a plugin by slug: filesystem preset bundle first, DB-published package as fallback."""
    plugin_dir = _resolve_plugin_dir(slug)
    if plugin_dir is not None:
        return normalize_plugin_dir(plugin_dir)
    if db is not None:
        row = _market_row(db, slug)
        if row is not None:
            with _extract_plugin_zip(base64.b64decode(row.package_b64)) as plugin_root:
                return normalize_plugin_dir(plugin_root)
    raise ResourceNotFoundError("plugin", slug)


def get_market_skill_exclusions(db: Optional[Session], slug: str) -> set:
    """The set of skill names the admin has "removed" from the marketplace for this plugin."""
    if db is None:
        return set()
    return {
        row[0]
        for row in db.query(PluginMarketSkillExclusion.skill_name)
        .filter(PluginMarketSkillExclusion.slug == slug)
        .all()
    }


def get_plugin_detail(slug: str, db: Optional[Session] = None) -> Dict[str, Any]:
    """Marketplace plugin detail (after normalize: component list + required secrets + drop preview).

    Filesystem preset bundle first; falls back to the DB marketplace package
    (admin-uploaded and published) when not found. Skills the admin removed
    from the marketplace (``PluginMarketSkillExclusion``) are not shown in the
    detail.
    """
    np = _normalize_market_plugin(slug, db)
    return _normalized_to_detail(np, excluded=get_market_skill_exclusions(db, slug))


def exclude_market_skill(
    db: Session, slug: str, skill_name: str, *, created_by: Optional[str] = None
) -> Dict[str, Any]:
    """"Remove" a skill from a marketplace plugin: record one exclusion (idempotent).

    Validates the skill actually belongs to this plugin (guards against
    typos); afterwards the marketplace list/detail/install no longer include
    it. Installed instances are untouched.
    """
    raw_names = {s.name for s in _normalize_market_plugin(slug, db).skills}
    if skill_name not in raw_names:
        raise BadRequestError(message=f"技能 {skill_name!r} 不属于插件 {slug!r}")
    existing = (
        db.query(PluginMarketSkillExclusion)
        .filter(
            PluginMarketSkillExclusion.slug == slug,
            PluginMarketSkillExclusion.skill_name == skill_name,
        )
        .first()
    )
    if existing is None:
        db.add(PluginMarketSkillExclusion(slug=slug, skill_name=skill_name, created_by=created_by))
        db.commit()
        logger.info("plugin_market_skill_excluded: slug=%s skill=%s", slug, skill_name)
    return {"slug": slug, "skill_name": skill_name, "excluded": True}


def _skill_component_preview(sk: NormalizedSkill) -> Dict[str, Any]:
    """Preview component for a not-yet-installed builtin/external skill: includes body instructions + file list for pre-install inspection."""
    from core.agent_skills.registry import _split_frontmatter
    desc = ""
    tags: List[str] = []
    try:
        fm, _ = _split_frontmatter(sk.skill_content or "")
        desc = (fm.get("description") or "").strip()
        raw_tags = fm.get("tags") or ""
        if isinstance(raw_tags, str) and raw_tags:
            tags = [x.strip() for x in raw_tags.replace("，", ",").split(",") if x.strip()]
    except Exception:  # noqa: BLE001
        pass
    files = sorted(k for k in (sk.extra_files or {}).keys() if k != "secrets.json")
    return {
        "skill_id": sk.name,
        "name": sk.name,
        "description": desc,
        "version": "",
        "tags": tags,
        "enabled": True,           # enabled by default after install (preview semantics)
        "instructions": _strip_frontmatter(sk.skill_content),
        "files": files,
        "has_secrets": False,
    }


def _normalized_to_detail(np: NormalizedPlugin, *, excluded: Optional[set] = None) -> Dict[str, Any]:
    excluded = excluded or set()
    return {
        "slug": np.slug,
        "name": np.name,
        "version": np.version,
        "description": np.description,
        "category": np.category,
        "icon": np.icon,
        "kind": np.kind,
        "required_secrets": np.required_secrets,
        "admin_config": _admin_config_view(np.admin_config, with_values=False),
        "connection": np.connection,
        "skills": [_skill_component_preview(s) for s in np.skills if s.name not in excluded],
        "mcp": [
            {
                "server_id": m.name, "name": m.name, "description": m.description,
                "transport": m.transport, "url": m.url, "enabled": not m.needs_runtime,
                "needs_runtime": m.needs_runtime, "note": m.note,
                "tools": list(getattr(m, "tools", None) or []),
            }
            for m in np.mcp
        ],
        "dropped": np.dropped,
    }


def install_plugin(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    secrets: Optional[Dict[str, str]] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Install a marketplace plugin package (filesystem default / marketplace, or a DB-published marketplace package)."""
    plugin_dir = _resolve_plugin_dir(slug)
    if plugin_dir is not None:
        np = normalize_plugin_dir(plugin_dir)
        return _apply_normalized(
            db, np, owner_user_id=owner_user_id, secrets=secrets or {},
            source="builtin", created_by=created_by,
        )
    # DB-published marketplace package: extract the original zip and go through the same path as import
    row = _market_row(db, slug)
    if row is not None:
        with _extract_plugin_zip(base64.b64decode(row.package_b64)) as plugin_root:
            return import_plugin(
                db, plugin_root, owner_user_id=owner_user_id,
                secrets=secrets or {}, created_by=created_by,
            )
    raise ResourceNotFoundError("plugin", slug)


def import_plugin(
    db: Session,
    plugin_dir: Path,
    *,
    owner_user_id: Optional[str],
    secrets: Optional[Dict[str, str]] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Import an external plugin directory (native / Claude Code / Codex), persist it, and return the import_report."""
    np = normalize_plugin_dir(plugin_dir)
    source = {"claude": "imported_claude", "codex": "imported_codex"}.get(np.kind, "builtin")
    return _apply_normalized(
        db, np, owner_user_id=owner_user_id, secrets=secrets or {},
        source=source, created_by=created_by,
    )


def _locate_plugin_root(extract_dir: Path) -> Path:
    """Locate the plugin root in the extraction dir: prefer the directory containing a manifest; zips often wrap an extra directory level."""
    def _has_manifest(d: Path) -> bool:
        return (
            (d / "plugin.json").is_file()
            or (d / ".claude-plugin" / "plugin.json").is_file()
            or (d / ".codex-plugin" / "plugin.json").is_file()
        )

    if _has_manifest(extract_dir):
        return extract_dir
    subdirs = [c for c in extract_dir.iterdir() if c.is_dir()]
    if len(subdirs) == 1 and _has_manifest(subdirs[0]):
        return subdirs[0]
    for d in sorted(extract_dir.rglob("*")):
        if d.is_dir() and _has_manifest(d):
            return d
    raise BadRequestError(message="zip 内未找到插件清单（plugin.json / .claude-plugin / .codex-plugin）")


@contextmanager
def _extract_plugin_zip(raw: bytes) -> Iterator[Path]:
    """Extract the plugin zip to a temp dir and locate the plugin root; yields that root dir and cleans up the temp dir on exit."""
    if len(raw) > MAX_ZIP_BYTES:
        raise BadRequestError(message=f"插件包过大（>{MAX_ZIP_BYTES // (1024 * 1024)}MB）")
    tmp_root = Path(tempfile.mkdtemp(prefix="plugin_import_"))
    try:
        extract_dir = tmp_root / "extracted"
        extract_dir.mkdir()
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for member in zf.namelist():
                    if member.startswith("/") or ".." in Path(member).parts:
                        raise BadRequestError(message=f"非法压缩包条目：{member}")
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            raise BadRequestError(message="不是有效的 zip 文件")
        yield _locate_plugin_root(extract_dir)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def import_plugin_from_zip(
    db: Session,
    raw: bytes,
    *,
    owner_user_id: Optional[str],
    secrets: Optional[Dict[str, str]] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Import a plugin from uploaded zip bytes (extract → locate root → import_plugin). Shared by user and admin uploads."""
    with _extract_plugin_zip(raw) as plugin_root:
        return import_plugin(
            db, plugin_root, owner_user_id=owner_user_id,
            secrets=secrets, created_by=created_by,
        )


# ── Plugin marketplace DB publishing (admin upload → persisted as an installable source; not installed, not globally in effect) ──────────

def _market_row(db: Session, slug: str) -> Optional[PluginMarketPackage]:
    if not slug:
        return None
    return db.query(PluginMarketPackage).filter(PluginMarketPackage.slug == slug).first()


def _market_meta_dict(row: PluginMarketPackage) -> Dict[str, Any]:
    """DB marketplace package → marketplace list metadata isomorphic to filesystem bundles."""
    return {
        "slug": row.slug,
        "name": row.name,
        "version": row.version or "1.0.0",
        "description": row.description or "",
        "category": row.category or "",
        "icon": row.icon,
        "skills_count": int(row.skills_count or 0),
        "required_secrets": list(row.required_secrets or []),
        "has_admin_config": bool(row.has_admin_config),
        "source": "uploaded",
    }


def publish_plugin_zip_to_market(
    db: Session, raw: bytes, *, created_by: Optional[str] = None
) -> Dict[str, Any]:
    """Admin uploads a plugin zip → published as a DB marketplace package (normalize validation + store the original zip); not installed.

    Once published it appears in the plugin marketplace list and can be
    explicitly installed by admins/users; re-uploading the same slug updates it.
    """
    with _extract_plugin_zip(raw) as plugin_root:
        np = normalize_plugin_dir(plugin_root)   # parse/validate; invalid input raises immediately, nothing persisted
    package_b64 = base64.b64encode(raw).decode("ascii")
    has_admin_config = bool(np.admin_config and (np.admin_config.get("fields")))
    now = datetime.utcnow()
    existing = _market_row(db, np.slug)
    fields = dict(
        name=np.name, version=np.version, description=np.description or "",
        category=np.category or "", icon=np.icon, kind=np.kind,
        skills_count=len(np.skills), required_secrets=list(np.required_secrets or []),
        has_admin_config=has_admin_config, package_b64=package_b64, updated_at=now,
    )
    if existing is not None:
        for key, val in fields.items():
            setattr(existing, key, val)
        flag_modified(existing, "required_secrets")
        action = "updated"
    else:
        db.add(PluginMarketPackage(slug=np.slug, created_at=now, created_by=created_by, **fields))
        action = "published"
    db.commit()
    logger.info("plugin_market_%s: slug=%s kind=%s skills=%d", action, np.slug, np.kind, len(np.skills))
    return {
        "slug": np.slug,
        "name": np.name,
        "kind": np.kind,
        "skills_count": len(np.skills),
        "action": action,
        "message": "插件已上架插件市场" if action == "published" else "插件市场内容已更新",
    }


def delete_market_package(db: Session, slug: str) -> Dict[str, Any]:
    """Remove an admin-uploaded DB marketplace package from the plugin marketplace (does not affect installed instances)."""
    row = _market_row(db, slug)
    if row is None:
        raise ResourceNotFoundError("plugin_market_package", slug)
    db.delete(row)
    db.commit()
    logger.info("plugin_market_deleted: slug=%s", slug)
    return {"slug": slug, "deleted": True}


def uninstall_plugin(db: Session, install_id: str, *, owner_user_id: Optional[str]) -> Dict[str, Any]:
    """Uninstall a plugin: precisely reverse-delete skills/MCP by source_plugin + owner, then delete the installation record."""
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    # Permissions: private plugins can only be uninstalled by their owner; global plugins require owner_user_id None (the route layer guarantees admin)
    if row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权卸载该插件")

    slug = row.slug

    def _owner_filter(model):
        return (
            (model.owner_user_id == owner_user_id)
            if owner_user_id is not None
            else model.owner_user_id.is_(None)
        )

    n_sk = db.query(AdminSkill).filter(
        AdminSkill.source_plugin == slug, _owner_filter(AdminSkill)
    ).delete(synchronize_session=False)
    n_mcp = db.query(AdminMcpServer).filter(
        AdminMcpServer.source_plugin == slug, _owner_filter(AdminMcpServer)
    ).delete(synchronize_session=False)

    db.delete(row)
    db.commit()
    _refresh_after_change(owner_user_id)
    logger.info("plugin_uninstalled: id=%s slug=%s skills=%d mcp=%d", install_id, slug, n_sk, n_mcp)
    return {"install_id": install_id, "slug": slug, "removed_skills": n_sk, "removed_mcp": n_mcp}


def set_plugin_enabled(
    db: Session, install_id: str, *, enabled: bool, owner_user_id: Optional[str]
) -> Dict[str, Any]:
    """Toggle a plugin as a whole: bulk-flip is_enabled on all of its skills/MCP.

    stdio MCP (non-empty command, no url) stays disabled even when enabling —
    it needs the runtime to be fully in place.
    """
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    if row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权操作该插件")

    cids = row.component_ids or {}
    skill_ids = cids.get("skills") or []
    server_ids = cids.get("mcp") or []
    if skill_ids:
        db.query(AdminSkill).filter(AdminSkill.skill_id.in_(skill_ids)).update(
            {AdminSkill.is_enabled: enabled}, synchronize_session=False
        )
    if server_ids:
        # Query all MCP servers at once, then decide row by row for stdio (needs runtime) — stdio stays disabled even when the plugin as a whole is enabled.
        for srv in db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(server_ids)).all():
            srv.is_enabled = bool(enabled) and srv.transport != "stdio"
    db.commit()
    _refresh_after_change(owner_user_id)
    return {"install_id": install_id, "enabled": enabled}


def set_plugin_component_enabled(
    db: Session,
    install_id: str,
    *,
    kind: str,
    component_id: str,
    enabled: bool,
    owner_user_id: Optional[str],
) -> Dict[str, Any]:
    """Individually toggle the global is_enabled of one component (skill / MCP) inside a plugin.

    Used by the plugin detail page's "per-skill/MCP management" — plugin skills
    no longer appear in the "Skill Management" list and are managed here
    instead. component_id must belong to this plugin's component_ids (prevents
    privilege escalation into modifying other plugins / hand-created skills).
    stdio MCP stays disabled even when enabling (needs the runtime fully in
    place), consistent with the whole-plugin toggle.
    """
    if kind not in ("skill", "mcp"):
        raise BadRequestError(message=f"不支持的组件类型：{kind}")
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    if row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权操作该插件")

    cids = row.component_ids or {}
    if kind == "skill":
        if component_id not in (cids.get("skills") or []):
            raise BadRequestError(message="该技能不属于此插件")
        sk = db.query(AdminSkill).filter(AdminSkill.skill_id == component_id).first()
        if sk is None:
            raise ResourceNotFoundError("admin_skill", component_id)
        sk.is_enabled = bool(enabled)
        effective = sk.is_enabled
    else:
        if component_id not in (cids.get("mcp") or []):
            raise BadRequestError(message="该 MCP 不属于此插件")
        srv = db.query(AdminMcpServer).filter(AdminMcpServer.server_id == component_id).first()
        if srv is None:
            raise ResourceNotFoundError("admin_mcp_server", component_id)
        # stdio MCP stays disabled even when enabling — needs the runtime fully in place.
        srv.is_enabled = bool(enabled) and srv.transport != "stdio"
        effective = srv.is_enabled
    db.commit()
    _refresh_after_change(owner_user_id)
    return {"install_id": install_id, "kind": kind, "component_id": component_id, "enabled": effective}


def set_plugin_enabled_for_user(
    db: Session, install_id: str, *, enabled: bool, user_id: str
) -> Dict[str, Any]:
    """A frontend user enables/disables a plugin **for themself** (including admin global plugins).

    Writes a per-user catalog override to each component (kind=skill/mcp)
    without touching the component's global is_enabled — so a user's toggle on
    a global plugin affects only them; it works the same for their own private
    plugins (_owned_enabled_ids honors overrides). Globally disabled components
    (e.g. stdio MCP) are protected by _merge_kind's admin-lock, so users cannot
    enable them beyond their privileges.
    """
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    # Allowed for one's own private plugin, or an admin global plugin (empty owner; users may toggle it for themselves).
    if row.owner_user_id is not None and row.owner_user_id != user_id:
        raise BadRequestError(message="无权操作该插件")

    from core.services.catalog_service import CatalogService
    svc = CatalogService(db)
    cids = row.component_ids or {}
    for sid in cids.get("skills") or []:
        svc.update_override(user_id, "skill", sid, enabled)
    for sid in cids.get("mcp") or []:
        svc.update_override(user_id, "mcp", sid, enabled)

    _refresh_after_change(user_id)
    return {"install_id": install_id, "enabled": enabled}
