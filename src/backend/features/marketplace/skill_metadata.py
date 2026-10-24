"""skill metadata responsibilities."""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional
from core.capabilities.skills.registry import split_frontmatter
from features.marketplace.models import MarketplaceSubmission
from core.infra.exceptions import BadRequestError
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.marketplace import skills_state

def read_manifest(slug: str) -> Optional[Dict[str, Any]]:
    """Read and validate a single market skill's ``marketplace.json`` (tolerant; returns None if corrupted)."""
    if not slug or "/" in slug or ".." in slug:
        return None
    path = skills_state.MARKETPLACE_DIR / slug / skills_state.MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("marketplace manifest broken: %s (%s)", slug, exc)
        return None
    if not isinstance(m, dict) or not m.get("entry_name"):
        return None
    m.setdefault("slug", slug)
    return m


def market_skill_requires_secrets(slug: str) -> bool:
    """Whether a preloaded market skill declares required_secrets (public contract, reusable by other markets/callers)."""
    m = read_manifest(slug)
    if not m:
        return False
    return bool(m.get("requires_api_key") or m.get("required_secrets"))


def public_meta(m: Dict[str, Any]) -> Dict[str, Any]:
    """Manifest → externally visible metadata (no skill body/file contents)."""
    return {
        "slug": m.get("slug"),
        "entry_name": m.get("entry_name"),
        "display_name": m.get("display_name") or m.get("slug"),
        "summary": m.get("summary") or "",
        "category": m.get("category") or "其他",
        "tags": list(m.get("tags") or []),
        "version": m.get("version") or "1.0.0",
        "author": m.get("author") or "skillhub",
        "icon_url": m.get("icon_url") or "",
        "source": m.get("source") or "skillhub",
        "source_url": m.get("source_url") or "",
        "downloads": int(m.get("downloads") or 0),
        "stars": int(m.get("stars") or 0),
        "featured": bool(m.get("featured")),
        "requires_api_key": bool(m.get("requires_api_key")),
        "required_secrets": list(m.get("required_secrets") or []),
        # Preloaded (filesystem) skills ship with the repo; admins cannot delete them online.
        "deletable": False,
    }


def submission_public_meta(sub: MarketplaceSubmission) -> Dict[str, Any]:
    """Approved community submission → external metadata isomorphic to the preloaded manifest form."""
    return {
        "slug": sub.slug,
        "entry_name": sub.slug,
        "display_name": sub.display_name or sub.slug,
        "summary": sub.summary or "",
        "category": sub.category or "社区共享",
        "tags": list(sub.tags or []),
        "version": sub.version or "1.0.0",
        "author": sub.submitter_name or sub.owner_user_id,
        "icon_url": "",
        "source": "community",
        "source_url": "",
        "downloads": 0,
        "stars": 0,
        "featured": False,
        "requires_api_key": False,
        "required_secrets": submission_required_secrets(sub),
        # DB listing records (admin uploads / user community submissions) → admins can delete online (remove from the market).
        "deletable": True,
    }


def approved_submissions(db: Session) -> List[MarketplaceSubmission]:
    return (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.status == "approved")
        .order_by(MarketplaceSubmission.created_at.desc())
        .all()
    )


def default_skill_public_meta(slug: str) -> Optional[Dict[str, Any]]:
    """Built-in default skill (extensions/skills/default/<slug>) → market-list metadata isomorphic to the preloaded form.

    The Chinese name comes from SKILL.md's ``display_name``; category/summary
    come from ``DEFAULT_SKILL_MARKET``. Marked ``source='builtin'`` +
    ``builtin=True`` — these skills are globally resident and always available
    to everyone; the market shows them as "built-in" (already available) and
    they do not go through "install as a private copy" (avoiding duplicating
    1MB+ office skills into the DB).
    """
    cfg = skills_state.DEFAULT_SKILL_MARKET.get(slug)
    if cfg is None:
        return None
    md = skills_state.DEFAULT_SKILLS_DIR / slug / skills_state.SKILL_MD_NAME
    if not md.is_file():
        return None
    try:
        fm, _ = split_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        fm = {}
    name = str(fm.get("name") or slug).strip()
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return {
        "slug": slug,
        "entry_name": name,
        "display_name": str(fm.get("display_name") or name).strip(),
        "summary": cfg["summary"],
        "category": cfg["category"],
        "tags": list(tags),
        "version": str(fm.get("version") or "1.0.0"),
        "author": "内置",
        "icon_url": "",
        "source": "builtin",
        "source_url": "",
        "downloads": 0,
        "stars": 0,
        "featured": False,
        "requires_api_key": False,
        "required_secrets": [],
        "deletable": False,  # built-ins ship with the repo; not deletable online
        "builtin": True,     # the frontend uses this to show "built-in" and set the button to "already built-in"
    }


def default_skill_metas() -> List[Dict[str, Any]]:
    metas = [default_skill_public_meta(s) for s in skills_state.DEFAULT_SKILL_MARKET]
    return [m for m in metas if m]


def validate_category(category: str) -> str:
    """Validate and return a legal category; anything outside the fixed set is a straight 400."""
    category = (category or "").strip()
    if category not in skills_state.MARKETPLACE_CATEGORIES:
        raise BadRequestError(
            message=f"请从固定分类中选择：{'、'.join(skills_state.MARKETPLACE_CATEGORIES)}"
        )
    return category


def get_approved_submission(db: Session, slug: str) -> Optional[MarketplaceSubmission]:
    return (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.slug == slug, MarketplaceSubmission.status == "approved")
        .first()
    )


def submission_required_secrets(sub: MarketplaceSubmission) -> List[Dict[str, Any]]:
    """Recover a community skill's required_secrets from the snapshot sentinel entry (empty list if absent)."""
    raw = (sub.extra_files or {}).get(skills_state.REQUIRED_SECRETS_SNAPSHOT)
    if not raw:
        return []
    try:
        fields = json.loads(raw)
        return fields if isinstance(fields, list) else []
    except (TypeError, ValueError):
        return []
