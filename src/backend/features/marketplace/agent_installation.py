"""agent installation responsibilities."""
from __future__ import annotations
from features.marketplace import skill_installation
from features.marketplace import skill_metadata
from typing import Any, Dict, List, Optional
from core.db.models import UserAgent
from core.infra.exceptions import ResourceNotFoundError
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.marketplace import agent_catalog
from features.marketplace import agent_metadata
from features.marketplace import agents_state

def installed_slugs(db: Session, owner_user_id: Optional[str], slugs: List[str]) -> set:
    """The subset of these marketplace slugs already installed (cloned) in the given scope (user / global admin)."""
    if not slugs:
        return set()
    q = db.query(UserAgent.source_market_slug).filter(UserAgent.source_market_slug.in_(slugs))
    if owner_user_id is None:
        q = q.filter(UserAgent.owner_type == "admin")
    else:
        q = q.filter(UserAgent.owner_type == "user", UserAgent.user_id == owner_user_id)
    return {row[0] for row in q.all()}


def annotate_installed(
    items: List[Dict[str, Any]], db: Session, owner_user_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Add the ``installed`` flag to each marketplace list item (for the given scope)."""
    if not items:
        return items
    installed = installed_slugs(db, owner_user_id, [it["slug"] for it in items])
    for it in items:
        it["installed"] = it["slug"] in installed
    return items


def is_installed(db: Session, slug: str, owner_user_id: Optional[str]) -> bool:
    return bool(installed_slugs(db, owner_user_id, [slug]))


def resolve_market_entry(db: Session, slug: str) -> Dict[str, Any]:
    """Normalize a preset bundle / community listing into a unified clone-source dict."""
    b = agent_catalog.read_bundle(slug)
    if b:
        return {
            "name": b.get("name") or slug,
            "avatar": b.get("avatar") or "",
            "description": b.get("description") or "",
            "summary": b.get("summary") or "",
            "system_prompt": b.get("system_prompt") or "",
            "welcome_message": b.get("welcome_message") or "",
            "suggested_questions": list(b.get("suggested_questions") or []),
            "model_config": dict(b.get("model_config") or {}),
            "bindings": agent_metadata.bindings_of(b),
            "ontology_tags": [
                str(tag)
                for tag in (b.get("ontology_tags") or b.get("tags") or [])
                if str(tag).startswith("ontology:")
            ],
        }
    sub = agent_catalog.get_approved_submission(db, slug)
    if sub is None:
        raise ResourceNotFoundError("marketplace_agent", slug)
    return {
        "name": sub.name or slug,
        "avatar": sub.avatar or "",
        "description": sub.description or "",
        "summary": sub.summary or "",
        "system_prompt": sub.system_prompt or "",
        "welcome_message": sub.welcome_message or "",
        "suggested_questions": list(sub.suggested_questions or []),
        "model_config": dict(sub.model_config_snapshot or {}),
        "bindings": agent_metadata.normalize_binding_lists(sub.bindings_snapshot),
        "ontology_tags": [str(tag) for tag in (sub.tags or []) if str(tag).startswith("ontology:")],
    }


def resolve_bindings(
    db: Session,
    bindings: Dict[str, List[str]],
    owner_user_id: Optional[str],
    operator_name: Optional[str],
) -> tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Resolve each marketplace binding id into the installer's scope; returns (final bindings, report).

    - MCP / default skills / accessible KBs: bind directly if within ``available-resources``;
    - marketplace skill slugs: if not in the available list, install as the installer's private skill and bind the returned id;
    - plugin slugs: bind if already installed, otherwise install as private and then bind;
    - unresolvable items go into ``dropped``; auto-installed skills/plugins requiring credentials go into ``needs_secret``.
    """
    from core.capabilities.catalog.api import get_enabled_ids
    from core.capabilities.plugins import management as plugin_service

    from core.capabilities.agents.edition import UserAgentService

    avail = UserAgentService(db).list_available_resources(owner_user_id=owner_user_id)
    avail_skill = {s["id"] for s in avail.get("skills", [])}
    # available-resources only lists AdminMcpServer rows; but built-in MCP tools (e.g.
    # database_query) are catalog-defined and loaded at runtime by catalog id,
    # and don't necessarily have a same-named AdminMcpServer row. Merge the catalog-enabled
    # built-in MCP ids into the "resolvable" set, so these legitimate built-in tools aren't
    # misjudged as "unresolvable" and dropped (once bound, the runtime connects on demand).
    avail_mcp = {s["id"] for s in avail.get("mcp_servers", [])} | set(get_enabled_ids("mcp"))
    avail_plugin = {p["id"] for p in avail.get("plugins", [])}
    avail_kb = {k["id"] for k in avail.get("kb_spaces", [])}

    final: Dict[str, List[str]] = {
        "skill_ids": [],
        "mcp_server_ids": [],
        "plugin_ids": [],
        "kb_ids": [],
    }
    report: Dict[str, List[str]] = {
        "bound": [],
        "installed": [],
        "dropped": [],
        "needs_secret": [],
    }

    # MCP tools
    for mid in bindings.get("mcp_server_ids", []):
        if mid in avail_mcp:
            final["mcp_server_ids"].append(mid)
            report["bound"].append(f"mcp:{mid}")
        else:
            report["dropped"].append(f"mcp:{mid}")

    # Skills (default skills bind directly; marketplace skills get auto-installed as private)
    for sid in bindings.get("skill_ids", []):
        if sid in avail_skill:
            final["skill_ids"].append(sid)
            report["bound"].append(f"skill:{sid}")
            continue
        try:
            res = skill_installation.install_marketplace_skill(db, sid, owner_user_id=owner_user_id, secrets={})
            final["skill_ids"].append(res["id"])
            report["installed"].append(f"skill:{sid}")
            if skill_metadata.market_skill_requires_secrets(sid):
                report["needs_secret"].append(f"skill:{sid}")
        except Exception as exc:  # noqa: BLE001
            logger.info("agent clone: skill binding unresolved %s (%s)", sid, exc)
            report["dropped"].append(f"skill:{sid}")

    # Plugins (bindings store the slug; bind the install_id if installed, otherwise auto-install)
    for pslug in bindings.get("plugin_ids", []):
        gid = f"{pslug}@global"
        match = (
            gid
            if gid in avail_plugin
            else next((p for p in avail_plugin if p.startswith(f"{pslug}@")), None)
        )
        if match:
            final["plugin_ids"].append(match)
            report["bound"].append(f"plugin:{pslug}")
            continue
        try:
            res = plugin_service.install_plugin(
                db, pslug, owner_user_id=owner_user_id, secrets={}, created_by=operator_name
            )
            final["plugin_ids"].append(res["install_id"])
            report["installed"].append(f"plugin:{pslug}")
            if res.get("required_secrets") or res.get("requires_secret"):
                report["needs_secret"].append(f"plugin:{pslug}")
        except Exception as exc:  # noqa: BLE001
            logger.info("agent clone: plugin binding unresolved %s (%s)", pslug, exc)
            report["dropped"].append(f"plugin:{pslug}")

    # Knowledge bases (keep only those the installer can access)
    for kid in bindings.get("kb_ids", []):
        if kid in avail_kb:
            final["kb_ids"].append(kid)
            report["bound"].append(f"kb:{kid}")
        else:
            report["dropped"].append(f"kb:{kid}")

    return final, report


def install_marketplace_agent(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    operator_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Install a marketplace sub-agent as a private clone (admin owner_user_id=None → global admin sub-agent)."""
    from core.capabilities.agents.edition import UserAgentService

    entry = resolve_market_entry(db, slug)
    final_bindings, report = resolve_bindings(db, entry["bindings"], owner_user_id, operator_name)

    mc = entry.get("model_config") or {}
    data: Dict[str, Any] = {
        "name": entry["name"],
        "avatar": entry.get("avatar") or None,
        "description": (entry.get("summary") or entry.get("description") or "")[:agents_state.MAX_DESC],
        "system_prompt": entry.get("system_prompt") or "",
        "welcome_message": entry.get("welcome_message") or "",
        "suggested_questions": list(entry.get("suggested_questions") or []),
        "skill_ids": final_bindings["skill_ids"],
        "mcp_server_ids": final_bindings["mcp_server_ids"],
        "plugin_ids": final_bindings["plugin_ids"],
        "kb_ids": final_bindings["kb_ids"],
        "source_market_slug": slug,
        "ontology_tags": list(entry.get("ontology_tags") or []),
    }
    if mc.get("temperature") is not None:
        data["temperature"] = mc["temperature"]
    if mc.get("max_tokens") is not None:
        data["max_tokens"] = mc["max_tokens"]
    if mc.get("max_iters") is not None:
        data["max_iters"] = mc["max_iters"]
    if mc.get("timeout") is not None:
        data["timeout"] = mc["timeout"]

    owner_type = "admin" if owner_user_id is None else "user"
    agent = UserAgentService(db).create(
        user_id=owner_user_id,
        operator_name=operator_name,
        owner_type=owner_type,
        data=data,
    )
    logger.info(
        "agent_market_install: slug=%s owner=%s agent=%s bound=%d installed=%d dropped=%d",
        slug,
        owner_user_id or "global",
        agent["agent_id"],
        len(report["bound"]),
        len(report["installed"]),
        len(report["dropped"]),
    )
    return {
        "agent_id": agent["agent_id"],
        "slug": slug,
        "owner": "self" if owner_user_id else "global",
        "install_report": report,
        "message": "子智能体已安装",
    }
