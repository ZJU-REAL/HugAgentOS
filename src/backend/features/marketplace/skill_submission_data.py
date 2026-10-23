"""skill submission data responsibilities."""
from __future__ import annotations
from core.capabilities.skills import installation as package_installation
import re
from typing import Any, Dict
from features.marketplace.models import MarketplaceSubmission
from sqlalchemy.orm import Session
from features.marketplace import skills_state

def strip_injected_secrets(skill_content: str) -> str:
    """Strip the "credentials configuration" section appended by _inject_secrets (including its preceding --- separator).

    Listing snapshots must be in the pre-injection form — the section instructs
    reading secrets.json, which the snapshot has already excluded; the
    installer's own credentials get a fresh section re-injected at install time.
    """
    idx = skill_content.find(skills_state.SECRETS_SECTION_HEADER)
    if idx < 0:
        return skill_content
    sep = skill_content.rfind("\n---", 0, idx)
    cut = sep if sep >= 0 else idx
    return skill_content[:cut].rstrip() + "\n"


def existing_slugs(db: Session) -> set:
    """Currently occupied market slugs: the preloaded directory + all submissions.

    Must include rejected rows — the slug column is table-wide UNIQUE and
    rejected submissions still occupy their slug; if derivation skipped them,
    "re-submitting after rejection" would hit the unique constraint and 500
    outright.
    """
    slugs = set()
    if skills_state.MARKETPLACE_DIR.is_dir():
        slugs.update(c.name for c in skills_state.MARKETPLACE_DIR.iterdir() if c.is_dir())
    rows = db.query(MarketplaceSubmission.slug).all()
    slugs.update(r[0] for r in rows)
    return slugs


def derive_slug(db: Session, skill_id: str, owner_user_id: str) -> str:
    """Derive the market slug from the source skill id: strip the user's own install-fingerprint suffix, then ensure global uniqueness."""
    base = skill_id
    suffix = f"-{package_installation.owner_suffix(owner_user_id)}"
    if base.endswith(suffix):
        base = base[: -len(suffix)]
    base = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-") or "skill"
    taken = existing_slugs(db)
    slug = base
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def submission_to_dict(sub: MarketplaceSubmission, *, with_content: bool = False) -> Dict[str, Any]:
    data = {
        "submission_id": sub.submission_id,
        "slug": sub.slug,
        "skill_id": sub.skill_id,
        "owner_user_id": sub.owner_user_id,
        "submitter_name": sub.submitter_name or "",
        "display_name": sub.display_name,
        "summary": sub.summary or "",
        "user_intro": sub.user_intro or "",
        "category": sub.category or "社区共享",
        "tags": list(sub.tags or []),
        "version": sub.version or "1.0.0",
        "note": sub.note or "",
        "status": sub.status,
        "review_note": sub.review_note or "",
        "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "file_count": len(sub.extra_files or {}),
    }
    if with_content:
        data["instructions"] = package_installation.strip_frontmatter(sub.skill_content or "")
        data["files"] = sorted((sub.extra_files or {}).keys())
    return data
