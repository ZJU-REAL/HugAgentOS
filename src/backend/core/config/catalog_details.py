"""User-facing skill/MCP metadata enrichment; never writes the static catalog."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.config.catalog_loader import (
    DB_HIDDEN_SERVERS,
    DB_UMBRELLA_ID,
    DB_UMBRELLA_NAME,
    DB_UMBRELLA_DESC,
    _database_query_capability_available,
)

_LOGGER = logging.getLogger(__name__)


def _private_skill_ids() -> set:
    """Set of private skill ids in admin_skills owned by some user (owner_user_id non-null).

    These skills don't enter the global catalog (injected per current user only by /v1/catalog),
    but the loader can still resolve / materialize / register them by id (owner verification
    happens at the request layer).
    """
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill

        with SessionLocal() as db:
            return {
                row[0]
                for row in db.query(AdminSkill.skill_id)
                .filter(AdminSkill.owner_user_id.isnot(None))
                .all()
            }
    except Exception:
        return set()


def skill_body_from_raw(raw: str) -> str:
    """Take the "body" from the full SKILL.md text as the detail fallback (frontmatter stripped).

    Externally imported skills usually have no separately written "user intro"; rather than
    showing "no details", show the user the full SKILL.md body directly. name/description/tags/version
    from the frontmatter are already displayed separately in the card header, so only the body
    (the markdown after ``---``) is taken here, avoiding rendering raw YAML. Without valid
    frontmatter, fall back to the full text; on empty/exception fall back to empty string
    (preserving the original "no details" behavior).
    """
    if not raw:
        return ""
    try:
        from core.agent_skills.registry import _split_frontmatter

        try:
            _, body = _split_frontmatter(raw)
        except Exception:
            # Without valid frontmatter, use the full text directly
            return raw.strip()
        return (body or "").strip()
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("skill body fallback parse failed: %s", e)
        return ""


def resolve_skill_detail(user_intro: Optional[str], raw: str) -> str:
    """Return the explicit user intro, or the SKILL.md body when it is blank.

    Whitespace-only input counts as empty.  Marketplace summaries and the
    developer-facing frontmatter description are deliberately not considered
    here: they are card metadata, not a substitute for a user introduction.
    """
    intro = str(user_intro or "").strip()
    return intro or skill_body_from_raw(raw)


def _skill_body_fallback(loader, sid: str) -> str:
    """Read the full SKILL.md by skill_id and take the body (detail fallback). See skill_body_from_raw."""
    try:
        raw = loader._backend.read_skill_file(sid)
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("skill body fallback read failed for %s: %s", sid, e)
        return ""
    return skill_body_from_raw(raw)


def _load_dynamic_skill_specs() -> Dict[str, Dict[str, Any]]:
    """Load dynamic skill metadata + user-facing intro markdown.

    The ``detail`` field returned here powers the capability-center detail
    page. It is **not** the raw SKILL.md body — that content is for the agent.
    Priority chain:

      1. ``AdminSkill.user_intro``      (admin override, highest priority)
      2. ``SKILL_USER_INTROS[sid]``     (built-in default in user_intros.py)
      3. SKILL.md body fallback         (externally imported / no intro: show the full skill text directly)
      4. ``""``                         (when even SKILL.md is unavailable, frontend shows "暂无详情")
    """
    try:
        from core.agent_skills.loader import get_skill_loader

        loader = get_skill_loader()
        # Refresh metadata cache to support hot-reload for bind-mounted skill files.
        loader.clear_cache()
        metadata_map = loader.load_all_metadata()
    except Exception as e:
        _LOGGER.warning(f"Failed to load dynamic skill specs: {e}")
        return {}

    # Admin DB overrides for user_intro (skill_id → markdown) + skill icon mapping
    db_user_intros: Dict[str, str] = {}
    skill_icons: Dict[str, str] = {}
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill
        from core.services.skill_icon_service import get_skill_icons

        with SessionLocal() as db:
            for sid, intro in db.query(AdminSkill.skill_id, AdminSkill.user_intro).all():
                normalized_intro = str(intro or "").strip()
                if normalized_intro:
                    db_user_intros[sid] = normalized_intro
            skill_icons = get_skill_icons(db)
    except Exception as e:
        _LOGGER.debug("Could not load admin skill user_intros/icons from DB: %s", e)

    try:
        from core.config.user_intros import SKILL_USER_INTROS
    except Exception:
        SKILL_USER_INTROS = {}

    _private_ids = _private_skill_ids()
    result: Dict[str, Dict[str, Any]] = {}
    for sid, metadata in metadata_map.items():
        if sid in _private_ids:
            continue  # private skills don't enter the global catalog (injected per user by /v1/catalog)
        detail = db_user_intros.get(sid) or SKILL_USER_INTROS.get(sid, "")
        if not detail:
            detail = _skill_body_fallback(loader, sid)
        result[sid] = {
            "id": sid,
            "name": metadata.name,
            "description": metadata.description,
            "version": metadata.version,
            "tags": metadata.tags,
            "detail": detail,
            "icon": skill_icons.get(sid, ""),
        }
    return result


def get_skill_curated_detail(skill_id: str) -> Optional[Dict[str, Any]]:
    """Return a single skill's "capability-center display" detail, structured like one item of _load_dynamic_skill_specs.

    For reuse by SSE/tool cards, avoiding stuffing the full SKILL.md into the frontend. Returns None if not found.
    """
    sid = (skill_id or "").strip()
    if not sid:
        return None
    try:
        specs = _load_dynamic_skill_specs()
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("get_skill_curated_detail: load failed: %s", e)
        return None
    spec = specs.get(sid)
    if spec:
        return dict(spec)
    # Not in the global catalog → most likely a private / marketplace-installed skill (excluded
    # by _private_skill_ids). Resolve metadata by id directly + detail fallback, so the
    # load_skill tool card can still display properly.
    return _curated_detail_for_owned(sid)


def _curated_detail_for_owned(sid: str) -> Optional[Dict[str, Any]]:
    """Build a "capability-center display" detail for a private/marketplace skill (bypassing the global catalog).

    detail prefers AdminSkill.user_intro, otherwise falls back to the SKILL.md body. Returns None if the skill is not found.
    """
    try:
        from core.agent_skills.loader import get_skill_loader

        loader = get_skill_loader()
        metadata = loader.load_all_metadata().get(sid)
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("_curated_detail_for_owned: metadata load failed for %s: %s", sid, e)
        return None
    if not metadata:
        return None

    user_intro = ""
    icon = ""
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill
        from core.services.skill_icon_service import get_skill_icons

        with SessionLocal() as db:
            row = db.query(AdminSkill.user_intro).filter(AdminSkill.skill_id == sid).first()
            if row and row[0]:
                user_intro = str(row[0]).strip()
            icon = get_skill_icons(db).get(sid, "")
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("_curated_detail_for_owned: DB lookup failed for %s: %s", sid, e)

    detail = user_intro or _skill_body_fallback(loader, sid)
    return {
        "id": sid,
        "name": metadata.name,
        "description": metadata.description,
        "version": metadata.version,
        "tags": metadata.tags,
        "detail": detail,
        "icon": icon,
    }


def _load_dynamic_mcp_specs() -> Dict[str, Dict[str, str]]:
    """Load dynamic MCP specs (names + user-facing intro).

    The ``detail`` field is **not** the raw server.py docstring listing — that
    is developer-facing. ``detail`` is the user_intro markdown shown in the
    capability-center detail page. Priority:

      1. ``AdminMcpServer.user_intro``       (admin override, highest)
      2. ``MCP_SERVER_USER_INTROS[sid]``     (built-in default)
      3. ``""``                              (frontend shows "暂无介绍")
    """
    try:
        from core.config.mcp_config import (
            MCP_SERVER_DESCRIPTIONS,
            MCP_SERVER_DISPLAY_NAMES,
            MCP_SERVERS,
        )
    except Exception as e:
        _LOGGER.warning(f"Failed to load MCP configs: {e}")
        return {}

    try:
        from core.config.user_intros import MCP_SERVER_USER_INTROS
    except Exception:
        MCP_SERVER_USER_INTROS = {}

    # Admin DB user_intro overrides apply regardless of is_enabled — even a
    # temporarily disabled server should still display the admin-curated
    # intro when re-enabled or browsed.
    db_user_intros: Dict[str, str] = {}
    # Enabled admin-only rows (i.e. server_ids NOT in MCP_SERVERS) get appended
    # for runtime detail lookups. They are not persisted into catalog.json.
    enabled_admin_only_rows: list = []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminMcpServer

        with SessionLocal() as db:
            # Only look at public MCPs (owner_user_id null). User-private MCPs don't enter the
            # global catalog; the /v1/catalog route injects them separately per current user.
            for row in (
                db.query(AdminMcpServer).filter(AdminMcpServer.owner_user_id.is_(None)).all()
            ):
                if row.user_intro:
                    db_user_intros[row.server_id] = row.user_intro
                if row.is_enabled and row.server_id not in MCP_SERVERS:
                    enabled_admin_only_rows.append(row)
    except Exception as e:
        _LOGGER.debug("Could not load admin MCP rows from DB: %s", e)

    result: Dict[str, Dict[str, str]] = {}
    for sid in MCP_SERVERS.keys():
        if sid in DB_HIDDEN_SERVERS:
            continue
        result[sid] = {
            "id": sid,
            "name": MCP_SERVER_DISPLAY_NAMES.get(sid, sid),
            "description": MCP_SERVER_DESCRIPTIONS.get(sid, f"MCP 服务：{sid}"),
            "detail": db_user_intros.get(sid) or MCP_SERVER_USER_INTROS.get(sid, ""),
        }

    # Append enabled admin-only MCP servers (those whose server_id isn't in the
    # static MCP_SERVERS dict) for runtime detail lookups only.
    for row in enabled_admin_only_rows:
        sid = row.server_id
        if sid in DB_HIDDEN_SERVERS:
            continue
        result[sid] = {
            "id": sid,
            "name": row.display_name or sid,
            "description": row.description or f"MCP 服务：{sid}",
            "detail": row.user_intro or MCP_SERVER_USER_INTROS.get(sid, ""),
        }

    # Inject the synthetic umbrella only when this edition ships a database
    # query runtime.  Otherwise the sync layer would recreate an item that the
    # CE build deliberately removed from catalog.json.
    if _database_query_capability_available():
        result[DB_UMBRELLA_ID] = {
            "id": DB_UMBRELLA_ID,
            "name": DB_UMBRELLA_NAME,
            "description": DB_UMBRELLA_DESC,
            "detail": MCP_SERVER_USER_INTROS.get(DB_UMBRELLA_ID, ""),
        }

    return result


def _attach_runtime_details(data: Dict[str, Any]) -> None:
    """Attach dynamic runtime details for skills and mcp without persisting."""
    skill_specs = _load_dynamic_skill_specs()
    skills_node = data.get("skills")
    if isinstance(skills_node, list):
        for item in skills_node:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id", "")).strip()
            spec = skill_specs.get(sid)
            if not spec:
                continue
            # Always sync name/description/version from dynamic source
            item["name"] = spec["name"]
            if spec["detail"]:
                item["detail"] = spec["detail"]
            if spec.get("icon"):
                item["icon"] = spec["icon"]
            item["description"] = spec["description"]
            item["desc"] = spec["description"]
            item["version"] = spec["version"]

    mcp_specs = _load_dynamic_mcp_specs()
    mcp_node = data.get("mcp")
    if isinstance(mcp_node, list):
        for item in mcp_node:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id", "")).strip()
            spec = mcp_specs.get(sid)
            if not spec:
                continue
            # Preserve explicit catalog.json labels/descriptions so manual edits
            # remain visible in the frontend. Runtime MCP metadata still fills
            # blanks and provides the dynamic detail block below.
            if not str(item.get("name", "")).strip():
                item["name"] = spec["name"]
            if not str(item.get("description", "")).strip():
                item["description"] = spec["description"]
            if not str(item.get("desc", "")).strip():
                item["desc"] = str(item.get("description") or spec["description"])
            if spec["detail"]:
                item["detail"] = spec["detail"]
            cfg = item.get("config")
            if not isinstance(cfg, dict):
                item["config"] = {"server": sid}
            elif not str(cfg.get("server", "")).strip():
                cfg["server"] = sid
