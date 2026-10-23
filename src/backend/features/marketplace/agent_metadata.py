"""agent metadata responsibilities."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from features.marketplace.models import AgentMarketSubmission
from features.marketplace.agent_categories import DEFAULT_AGENT_CATEGORY

def normalize_binding_lists(raw: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Normalize a bindings snapshot into the four id lists (missing ones become empty)."""
    b = raw or {}
    return {
        "skill_ids": list(b.get("skill_ids") or []),
        "mcp_server_ids": list(b.get("mcp_server_ids") or []),
        "plugin_ids": list(b.get("plugin_ids") or []),
        "kb_ids": list(b.get("kb_ids") or []),
    }


def bindings_of(d: Dict[str, Any]) -> Dict[str, List[str]]:
    """Get the four binding id lists from a preset bundle's ``bindings``."""
    return normalize_binding_lists(d.get("bindings"))


def binding_counts(bindings: Dict[str, List[str]]) -> Dict[str, int]:
    return {
        "skill_count": len(bindings.get("skill_ids") or []),
        "mcp_count": len(bindings.get("mcp_server_ids") or []),
        "plugin_count": len(bindings.get("plugin_ids") or []),
        "kb_count": len(bindings.get("kb_ids") or []),
    }


def make_market_meta(
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
        **binding_counts(bindings),
    }


def bundle_public_meta(b: Dict[str, Any]) -> Dict[str, Any]:
    # Presets (filesystem) ship with the repo; admins cannot delete them online (deletable=False).
    return make_market_meta(
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
        bindings=bindings_of(b),
    )


def submission_public_meta(sub: AgentMarketSubmission) -> Dict[str, Any]:
    # DB listing records (admin upload / user community submission) → admins can delete online (deletable=True).
    return make_market_meta(
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
        bindings=normalize_binding_lists(sub.bindings_snapshot),
    )
