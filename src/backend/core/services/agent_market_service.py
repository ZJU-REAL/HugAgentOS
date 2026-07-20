"""Sub-Agent Marketplace service.

The sub-agent marketplace replicates the skill marketplace's "user submits → admin reviews →
everyone can install" loop, but the marketplace items are **sub-agents** (role prompt +
capability bindings) rather than skill files:

- Preset sub-agents: shipped with the repo, stored at ``agent_bundles/marketplace/<slug>/agent.json``
  (pure data, no attachments). The initial content was curated and adapted from Cherry Studio.
- Community listings (``agent_market_submissions``): users **submit** their self-built sub-agents
  for listing; once an admin approves, they appear in the marketplace with ``source=community``
  and anyone can install. A content snapshot of the source ``UserAgent`` is taken at submit time.

Install semantics = **clone as a private sub-agent**: create a new ``UserAgent`` under the
installer (``owner_type=user``; admin installs get ``owner_type=admin``, global), copying the
prompt/welcome message/suggested questions/model config, and applying "install-on-demand" to
capability bindings — MCP/default skills bind directly, marketplace skills/plugins are installed
as the installer's private copies as needed and then bound, and unresolvable items are dropped.
The clone records ``source_market_slug`` to mark it "installed".
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db.models import AgentMarketSubmission, UserAgent
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services import marketplace_listing as ml
from core.services.agent_market_categories import (
    AGENT_MARKETPLACE_CATEGORIES,
    DEFAULT_AGENT_CATEGORY,
    validate_category,
)
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

# Preset sub-agent root directory: ``src/backend/agent_bundles/marketplace/<slug>/agent.json``.
AGENT_MARKET_DIR = Path(__file__).resolve().parents[2] / "agent_bundles" / "marketplace"
AGENT_JSON = "agent.json"

# Sentinel owner for admin direct uploads to the marketplace (distinguishes "admin upload" from real users' community submissions).
ADMIN_UPLOAD_OWNER = "__admin_upload__"

# The clone's description field matches the user-side creation form's constraint (≤20 chars); the full copy lives in system_prompt.
_MAX_DESC = 20


# ── Preset bundle reading ────────────────────────────────────────────────────


def _read_bundle(slug: str) -> Optional[Dict[str, Any]]:
    """Read and validate a single preset sub-agent's ``agent.json`` (fault-tolerant; returns None if broken)."""
    if not slug or "/" in slug or ".." in slug:
        return None
    path = AGENT_MARKET_DIR / slug / AGENT_JSON
    if not path.is_file():
        return None
    try:
        b = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent market bundle broken: %s (%s)", slug, exc)
        return None
    if not isinstance(b, dict) or not b.get("name"):
        return None
    b.setdefault("slug", slug)
    return b


def _normalize_binding_lists(raw: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Normalize a bindings snapshot into the four id lists (missing ones become empty)."""
    b = raw or {}
    return {
        "skill_ids": list(b.get("skill_ids") or []),
        "mcp_server_ids": list(b.get("mcp_server_ids") or []),
        "plugin_ids": list(b.get("plugin_ids") or []),
        "kb_ids": list(b.get("kb_ids") or []),
    }


def _bindings_of(d: Dict[str, Any]) -> Dict[str, List[str]]:
    """Get the four binding id lists from a preset bundle's ``bindings``."""
    return _normalize_binding_lists(d.get("bindings"))


def _binding_counts(bindings: Dict[str, List[str]]) -> Dict[str, int]:
    return {
        "skill_count": len(bindings.get("skill_ids") or []),
        "mcp_count": len(bindings.get("mcp_server_ids") or []),
        "plugin_count": len(bindings.get("plugin_ids") or []),
        "kb_count": len(bindings.get("kb_ids") or []),
    }


def _make_market_meta(
    *,
    slug: str,
    name: str,
    avatar: str,
    summary: str,
    description: str,
    category: str,
    tags: Any,
    version: str,
    author: str,
    source: str,
    featured: bool,
    deletable: bool,
    bindings: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Preset bundle / community listing → isomorphic public list metadata (without the system_prompt body)."""
    return {
        "slug": slug,
        "name": name or slug,
        "avatar": avatar or "",
        "summary": summary or "",
        "description": description or "",
        "category": category or DEFAULT_AGENT_CATEGORY,
        "tags": list(tags or []),
        "version": version or "1.0.0",
        "author": author,
        "source": source,
        "featured": featured,
        "deletable": deletable,
        **_binding_counts(bindings),
    }


def _bundle_public_meta(b: Dict[str, Any]) -> Dict[str, Any]:
    # Presets (filesystem) ship with the repo; admins cannot delete them online (deletable=False).
    return _make_market_meta(
        slug=b.get("slug"),
        name=b.get("name"),
        avatar=b.get("avatar"),
        summary=b.get("summary"),
        description=b.get("description"),
        category=b.get("category"),
        tags=b.get("tags"),
        version=b.get("version"),
        author=b.get("author") or "内置",
        source="builtin",
        featured=bool(b.get("featured")),
        deletable=False,
        bindings=_bindings_of(b),
    )


def _submission_public_meta(sub: AgentMarketSubmission) -> Dict[str, Any]:
    # DB listing records (admin upload / user community submission) → admins can delete online (deletable=True).
    return _make_market_meta(
        slug=sub.slug,
        name=sub.name,
        avatar=sub.avatar,
        summary=sub.summary,
        description=sub.description,
        category=sub.category,
        tags=sub.tags,
        version=sub.version,
        author=sub.submitter_name or sub.owner_user_id,
        source="community",
        featured=False,
        deletable=True,
        bindings=_normalize_binding_lists(sub.bindings_snapshot),
    )


def _approved_submissions(db: Session) -> List[AgentMarketSubmission]:
    return (
        db.query(AgentMarketSubmission)
        .filter(AgentMarketSubmission.status == "approved")
        .order_by(AgentMarketSubmission.created_at.desc())
        .all()
    )


def _get_approved_submission(db: Session, slug: str) -> Optional[AgentMarketSubmission]:
    return (
        db.query(AgentMarketSubmission)
        .filter(
            AgentMarketSubmission.slug == slug,
            AgentMarketSubmission.status == "approved",
        )
        .first()
    )


# ── List / categories / detail ───────────────────────────────────────────────


def list_marketplace_agents(
    db: Session, *, include_disabled: bool = False, viewer_user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List all marketplace sub-agents: presets (featured first) + community listings, layered with the enable/disable switch and visibility-scope filtering."""
    items: List[Dict[str, Any]] = []
    if AGENT_MARKET_DIR.is_dir():
        for child in sorted(AGENT_MARKET_DIR.iterdir()):
            if not child.is_dir():
                continue
            b = _read_bundle(child.name)
            if b:
                items.append(_bundle_public_meta(b))
    items.sort(key=lambda x: (0 if x["featured"] else 1, x["slug"]))
    items.extend(_submission_public_meta(s) for s in _approved_submissions(db))
    items = ml.annotate_and_filter(
        db,
        ml.KIND_AGENT,
        items,
        id_key="slug",
        include_disabled=include_disabled,
        viewer_user_id=viewer_user_id,
    )
    return items


def categories_from_items(items: List[Dict[str, Any]]) -> List[str]:
    """Derive categories from an already-fetched marketplace list: the fixed 9 major categories first, legacy leftover categories appended after."""
    seen: List[str] = list(AGENT_MARKETPLACE_CATEGORIES)
    for it in items:
        if it["category"] not in seen:
            seen.append(it["category"])
    return seen


def list_categories(
    db: Session, *, include_disabled: bool = False, viewer_user_id: Optional[str] = None
) -> List[str]:
    """Marketplace category list (standalone entry point; the list endpoint should reuse categories_from_items to avoid a second scan)."""
    return categories_from_items(
        list_marketplace_agents(
            db, include_disabled=include_disabled, viewer_user_id=viewer_user_id
        )
    )


def get_agent_detail(db: Session, slug: str) -> Dict[str, Any]:
    """Detail of a single marketplace sub-agent: metadata + prompt body + capability bindings (for the detail preview).

    The preset directory takes priority; falls back to the community listing (approved submission) if not found.
    """
    b = _read_bundle(slug)
    if b:
        data = _bundle_public_meta(b)
        data["system_prompt"] = b.get("system_prompt") or ""
        data["welcome_message"] = b.get("welcome_message") or ""
        data["suggested_questions"] = list(b.get("suggested_questions") or [])
        data["bindings"] = _bindings_of(b)
        return data
    sub = _get_approved_submission(db, slug)
    if sub is None:
        raise ResourceNotFoundError("marketplace_agent", slug)
    data = _submission_public_meta(sub)
    data["system_prompt"] = sub.system_prompt or ""
    data["welcome_message"] = sub.welcome_message or ""
    data["suggested_questions"] = list(sub.suggested_questions or [])
    data["bindings"] = _normalize_binding_lists(sub.bindings_snapshot)
    return data


def _installed_slugs(db: Session, owner_user_id: Optional[str], slugs: List[str]) -> set:
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
    installed = _installed_slugs(db, owner_user_id, [it["slug"] for it in items])
    for it in items:
        it["installed"] = it["slug"] in installed
    return items


def is_installed(db: Session, slug: str, owner_user_id: Optional[str]) -> bool:
    return bool(_installed_slugs(db, owner_user_id, [slug]))


# ── Install (clone + install-on-demand dependencies) ─────────────────────────


def _resolve_market_entry(db: Session, slug: str) -> Dict[str, Any]:
    """Normalize a preset bundle / community listing into a unified clone-source dict."""
    b = _read_bundle(slug)
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
            "bindings": _bindings_of(b),
            "ontology_tags": [
                str(tag)
                for tag in (b.get("ontology_tags") or b.get("tags") or [])
                if str(tag).startswith("ontology:")
            ],
        }
    sub = _get_approved_submission(db, slug)
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
        "bindings": _normalize_binding_lists(sub.bindings_snapshot),
        "ontology_tags": [str(tag) for tag in (sub.tags or []) if str(tag).startswith("ontology:")],
    }


def _resolve_bindings(
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
    from core.config.catalog import get_enabled_ids
    from core.services import marketplace_service as mk
    from core.services import plugin_service
    from core.services.user_agent_service import UserAgentService

    avail = UserAgentService(db).list_available_resources(owner_user_id=owner_user_id)
    avail_skill = {s["id"] for s in avail.get("skills", [])}
    # available-resources only lists AdminMcpServer rows; but built-in MCP tools (e.g.
    # database_query / report_export_mcp) are catalog-defined and loaded at runtime by catalog id,
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
            res = mk.install_marketplace_skill(db, sid, owner_user_id=owner_user_id, secrets={})
            final["skill_ids"].append(res["id"])
            report["installed"].append(f"skill:{sid}")
            if mk.market_skill_requires_secrets(sid):
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
    from core.services.user_agent_service import UserAgentService

    entry = _resolve_market_entry(db, slug)
    final_bindings, report = _resolve_bindings(db, entry["bindings"], owner_user_id, operator_name)

    mc = entry.get("model_config") or {}
    data: Dict[str, Any] = {
        "name": entry["name"],
        "avatar": entry.get("avatar") or None,
        "description": (entry.get("summary") or entry.get("description") or "")[:_MAX_DESC],
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


# ── Community listing: user submission + admin review ───────────────────────


def _existing_slugs(db: Session) -> set:
    """Marketplace slugs currently taken: preset directory + all submissions (including rejected; slugs are table-wide unique)."""
    slugs = set()
    if AGENT_MARKET_DIR.is_dir():
        slugs.update(c.name for c in AGENT_MARKET_DIR.iterdir() if c.is_dir())
    slugs.update(r[0] for r in db.query(AgentMarketSubmission.slug).all())
    return slugs


def _derive_slug(db: Session, name: str) -> str:
    """Derive the marketplace slug from the sub-agent name (ASCII-ized + unique); Chinese names fall back to agent-<hex>."""
    base = re.sub(r"[^a-z0-9_-]+", "-", (name or "").lower()).strip("-")
    if not base:
        base = f"agent-{uuid.uuid4().hex[:8]}"
    taken = _existing_slugs(db)
    slug = base
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _model_config_snapshot(agent: UserAgent) -> Dict[str, Any]:
    return {
        "temperature": float(agent.temperature) if agent.temperature is not None else None,
        "max_tokens": agent.max_tokens,
        "max_iters": agent.max_iters,
        "timeout": agent.timeout,
    }


def _bindings_snapshot(agent: UserAgent) -> Dict[str, List[str]]:
    return {
        "skill_ids": list(agent.skill_ids or []),
        "mcp_server_ids": list(agent.mcp_server_ids or []),
        "plugin_ids": list(agent.plugin_ids or []),
        "kb_ids": list(agent.kb_ids or []),
    }


def _submission_to_dict(
    sub: AgentMarketSubmission, *, with_content: bool = False
) -> Dict[str, Any]:
    data = {
        "submission_id": sub.submission_id,
        "slug": sub.slug,
        "agent_id": sub.agent_id,
        "owner_user_id": sub.owner_user_id,
        "submitter_name": sub.submitter_name or "",
        "name": sub.name,
        "avatar": sub.avatar or "",
        "description": sub.description or "",
        "summary": sub.summary or "",
        "category": sub.category or DEFAULT_AGENT_CATEGORY,
        "tags": list(sub.tags or []),
        "version": sub.version or "1.0.0",
        "note": sub.note or "",
        "status": sub.status,
        "review_note": sub.review_note or "",
        "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }
    if with_content:
        data["system_prompt"] = sub.system_prompt or ""
        data["welcome_message"] = sub.welcome_message or ""
        data["suggested_questions"] = list(sub.suggested_questions or [])
        data["bindings"] = _normalize_binding_lists(sub.bindings_snapshot)
    return data


def _owned_agent(db: Session, agent_id: str, owner_user_id: str) -> UserAgent:
    """Fetch the sub-agent owned by this user (owner_type=user), otherwise 404."""
    agent = (
        db.query(UserAgent)
        .filter(
            UserAgent.agent_id == agent_id,
            UserAgent.owner_type == "user",
            UserAgent.user_id == owner_user_id,
        )
        .first()
    )
    if agent is None:
        raise ResourceNotFoundError("user_agent", agent_id)
    return agent


def _new_submission(
    *,
    submission_id: str,
    slug: str,
    agent_id: str,
    owner_user_id: str,
    name: str,
    category: str,
    status: str,
    submitter_name: str = "",
    avatar: Optional[str] = None,
    description: str = "",
    summary: str = "",
    note: str = "",
    system_prompt: str = "",
    welcome_message: str = "",
    suggested_questions: Optional[List[str]] = None,
    model_config_snapshot: Optional[Dict[str, Any]] = None,
    bindings_snapshot: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
    reviewed_at: Optional[datetime] = None,
) -> AgentMarketSubmission:
    """Uniformly construct one ``AgentMarketSubmission`` (fixed fields and default normalization centralized here).

    The three entry points (user submission ``submit_to_marketplace`` / the create branch of the
    admin direct-upload ``publish_agent_to_market`` / admin create-and-list
    ``create_market_agent``) share the same field set; callers pass only what differs.
    """
    now = datetime.utcnow()
    return AgentMarketSubmission(
        submission_id=submission_id,
        slug=slug,
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        submitter_name=(submitter_name or "").strip(),
        name=name,
        avatar=avatar,
        description=description or "",
        summary=summary or "",
        category=category,
        tags=list(tags or []),
        version="1.0.0",
        note=(note or "").strip(),
        system_prompt=system_prompt or "",
        welcome_message=welcome_message or "",
        suggested_questions=list(suggested_questions or []),
        model_config_snapshot=dict(model_config_snapshot or {}),
        bindings_snapshot=_normalize_binding_lists(bindings_snapshot),
        status=status,
        reviewed_at=reviewed_at,
        created_at=now,
        updated_at=now,
    )


def submit_to_marketplace(
    db: Session,
    agent_id: str,
    *,
    owner_user_id: str,
    submitter_name: str = "",
    note: str = "",
    category: str = "",
    summary: str = "",
) -> Dict[str, Any]:
    """A user submits their self-built sub-agent for marketplace listing (content snapshot, pending admin review)."""
    agent = _owned_agent(db, agent_id, owner_user_id)

    active = (
        db.query(AgentMarketSubmission)
        .filter(
            AgentMarketSubmission.agent_id == agent_id,
            AgentMarketSubmission.owner_user_id == owner_user_id,
            AgentMarketSubmission.status.in_(["pending", "approved"]),
        )
        .first()
    )
    if active is not None:
        state = "待审核" if active.status == "pending" else "已上架"
        raise HTTPException(status_code=409, detail=f"该子智能体已有{state}的上架申请")

    sub = _new_submission(
        submission_id=f"asub_{uuid.uuid4().hex[:16]}",
        slug=_derive_slug(db, agent.name),
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        submitter_name=submitter_name,
        name=agent.name,
        avatar=agent.avatar,
        description=agent.description or "",
        summary=(summary or "").strip() or (agent.description or ""),
        category=validate_category(category),
        note=note,
        system_prompt=agent.system_prompt or "",
        welcome_message=agent.welcome_message or "",
        suggested_questions=agent.suggested_questions,
        model_config_snapshot=_model_config_snapshot(agent),
        bindings_snapshot=_bindings_snapshot(agent),
        tags=list((agent.extra_config or {}).get("ontology_tags") or []),
        status="pending",
    )
    db.add(sub)
    db.commit()
    logger.info(
        "agent_market_submission_created: id=%s agent=%s owner=%s slug=%s",
        sub.submission_id,
        agent_id,
        owner_user_id,
        sub.slug,
    )
    return _submission_to_dict(sub)


def list_my_submissions(db: Session, owner_user_id: str) -> List[Dict[str, Any]]:
    rows = (
        db.query(AgentMarketSubmission)
        .filter(AgentMarketSubmission.owner_user_id == owner_user_id)
        .order_by(AgentMarketSubmission.created_at.desc())
        .all()
    )
    return [_submission_to_dict(r) for r in rows]


def withdraw_submission(db: Session, submission_id: str, owner_user_id: str) -> None:
    """A user withdraws their own submission: pending/rejected can be deleted; approved requires an admin to delist."""
    sub = (
        db.query(AgentMarketSubmission)
        .filter(
            AgentMarketSubmission.submission_id == submission_id,
            AgentMarketSubmission.owner_user_id == owner_user_id,
        )
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("agent_market_submission", submission_id)
    if sub.status == "approved":
        raise BadRequestError(message="子智能体已上架，如需下架请联系管理员")
    db.delete(sub)
    db.commit()


def list_submissions(db: Session, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Admin view: all listing submissions, filterable by status (pending first, newest → oldest)."""
    q = db.query(AgentMarketSubmission)
    if status:
        q = q.filter(AgentMarketSubmission.status == status)
    rows = q.order_by(AgentMarketSubmission.created_at.desc()).all()
    order = {"pending": 0, "approved": 1, "rejected": 2}
    rows.sort(key=lambda r: order.get(r.status, 3))
    return [_submission_to_dict(r) for r in rows]


def get_submission(db: Session, submission_id: str) -> Dict[str, Any]:
    sub = (
        db.query(AgentMarketSubmission)
        .filter(AgentMarketSubmission.submission_id == submission_id)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("agent_market_submission", submission_id)
    return _submission_to_dict(sub, with_content=True)


def review_submission(
    db: Session,
    submission_id: str,
    *,
    approve: bool,
    review_note: str = "",
    category: str = "",
) -> Dict[str, Any]:
    """Admin review: approval lists it immediately; rejecting pending = refuse, rejecting approved = delist."""
    sub = (
        db.query(AgentMarketSubmission)
        .filter(AgentMarketSubmission.submission_id == submission_id)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("agent_market_submission", submission_id)
    if approve and sub.status != "pending":
        state = "已上架" if sub.status == "approved" else "已驳回"
        raise HTTPException(
            status_code=409,
            detail=f"该申请{state}，不能重复审核通过（如需重新上架请让用户重新提交）",
        )
    if not approve and sub.status == "rejected":
        raise HTTPException(status_code=409, detail="该申请已是驳回状态")
    sub.status = "approved" if approve else "rejected"
    sub.review_note = (review_note or "").strip()
    if approve and (category or "").strip():
        sub.category = validate_category(category)
    sub.reviewed_at = datetime.utcnow()
    sub.updated_at = datetime.utcnow()
    db.commit()
    logger.info(
        "agent_market_submission_reviewed: id=%s slug=%s status=%s",
        sub.submission_id,
        sub.slug,
        sub.status,
    )
    return _submission_to_dict(sub)


# ── Admin direct upload + delete + list/delist ───────────────────────────────


def publish_agent_to_market(
    db: Session,
    agent_id: str,
    *,
    category: str,
    summary: str = "",
    submitter_name: str = "管理员上传",
) -> Dict[str, Any]:
    """An admin lists an existing sub-agent directly on the marketplace as approved (no review).

    If the same agent_id already has an admin listing record, update it (keeping the original slug); otherwise create a new one.
    """
    category = validate_category(category)
    agent = db.query(UserAgent).filter(UserAgent.agent_id == agent_id).first()
    if agent is None:
        raise ResourceNotFoundError("user_agent", agent_id)

    now = datetime.utcnow()
    existing = (
        db.query(AgentMarketSubmission)
        .filter(
            AgentMarketSubmission.agent_id == agent_id,
            AgentMarketSubmission.owner_user_id == ADMIN_UPLOAD_OWNER,
        )
        .first()
    )
    summary = (summary or "").strip() or (agent.description or "")
    if existing is not None:
        existing.name = agent.name
        existing.avatar = agent.avatar
        existing.description = agent.description or ""
        existing.summary = summary
        existing.category = category
        existing.system_prompt = agent.system_prompt or ""
        existing.welcome_message = agent.welcome_message or ""
        existing.suggested_questions = list(agent.suggested_questions or [])
        existing.model_config_snapshot = _model_config_snapshot(agent)
        existing.bindings_snapshot = _bindings_snapshot(agent)
        existing.tags = list((agent.extra_config or {}).get("ontology_tags") or [])
        existing.status = "approved"
        existing.review_note = ""
        existing.reviewed_at = now
        existing.updated_at = now
        flag_modified(existing, "suggested_questions")
        flag_modified(existing, "model_config_snapshot")
        flag_modified(existing, "bindings_snapshot")
        flag_modified(existing, "tags")
        db.commit()
        return {"slug": existing.slug, "action": "updated", "message": "子智能体市场内容已更新"}

    sub = _new_submission(
        submission_id=f"aadm_{uuid.uuid4().hex[:16]}",
        slug=_derive_slug(db, agent.name),
        agent_id=agent_id,
        owner_user_id=ADMIN_UPLOAD_OWNER,
        submitter_name=submitter_name,
        name=agent.name,
        avatar=agent.avatar,
        description=agent.description or "",
        summary=summary,
        category=category,
        note="管理员上传，免审核直接上架",
        system_prompt=agent.system_prompt or "",
        welcome_message=agent.welcome_message or "",
        suggested_questions=agent.suggested_questions,
        model_config_snapshot=_model_config_snapshot(agent),
        bindings_snapshot=_bindings_snapshot(agent),
        tags=list((agent.extra_config or {}).get("ontology_tags") or []),
        status="approved",
        reviewed_at=now,
    )
    db.add(sub)
    db.commit()
    logger.info("agent_market_admin_published: agent=%s slug=%s", agent_id, sub.slug)
    return {"slug": sub.slug, "action": "published", "message": "子智能体已上架市场"}


def create_market_agent(
    db: Session,
    *,
    name: str,
    category: str,
    avatar: str = "",
    description: str = "",
    summary: str = "",
    system_prompt: str = "",
    welcome_message: str = "",
    suggested_questions: Optional[List[str]] = None,
    bindings: Optional[Dict[str, Any]] = None,
    model_config: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
    submitter_name: str = "管理员上传",
) -> Dict[str, Any]:
    """An admin directly "creates" a sub-agent and lists it on the marketplace as approved (no global agent created).

    Mirrors the skill side's "creating a skill lists it on the skill marketplace": the new
    sub-agent enters the sub-agent marketplace (explicitly installable as a global / private
    clone) and does not take effect globally right away. Content comes from form fields rather
    than a source ``UserAgent``, hence a synthetic placeholder ``agent_id`` (installs always use
    the snapshot and never look back at a source agent).
    """
    now = datetime.utcnow()
    sub = _new_submission(
        submission_id=f"aadm_{uuid.uuid4().hex[:16]}",
        slug=_derive_slug(db, name),
        agent_id=f"__market__{uuid.uuid4().hex[:12]}",
        owner_user_id=ADMIN_UPLOAD_OWNER,
        submitter_name=submitter_name,
        name=name,
        avatar=avatar or "",
        description=description or "",
        summary=(summary or "").strip() or (description or ""),
        category=validate_category(category),
        note="管理员创建，直接上架",
        system_prompt=system_prompt or "",
        welcome_message=welcome_message or "",
        suggested_questions=suggested_questions,
        model_config_snapshot=model_config,
        bindings_snapshot=bindings,
        tags=tags,
        status="approved",
        reviewed_at=now,
    )
    db.add(sub)
    db.commit()
    logger.info("agent_market_admin_created: slug=%s name=%s", sub.slug, name)
    return {"slug": sub.slug, "action": "created", "message": "子智能体已上架子智能体市场"}


def delete_market_agent(db: Session, slug: str) -> Dict[str, Any]:
    """An admin deletes one DB listing record from the marketplace. Presets ship with the repo and cannot be deleted online (400)."""
    if _read_bundle(slug) is not None:
        raise BadRequestError(message="预置子智能体随仓库发布，不可在线删除")
    sub = db.query(AgentMarketSubmission).filter(AgentMarketSubmission.slug == slug).first()
    if sub is None:
        raise ResourceNotFoundError("marketplace_agent", slug)
    db.delete(sub)
    db.commit()
    logger.info("agent_market_deleted: slug=%s", slug)
    return {"slug": slug, "deleted": True}


def set_market_agent_enabled(
    db: Session, slug: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    """List/delist a marketplace sub-agent (controls whether it shows in the marketplace; installed clones are unaffected)."""
    return ml.set_listing_enabled(db, ml.KIND_AGENT, slug, enabled, updated_by=updated_by)
