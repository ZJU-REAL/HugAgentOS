"""skill catalog responsibilities."""
from __future__ import annotations
from core.capabilities.skills import installation as package_installation
from typing import Any, Dict, List, Optional
from core.db.models import AdminSkill
from core.infra.exceptions import ResourceNotFoundError
from sqlalchemy.orm import Session
from features.marketplace import skill_metadata
from features.marketplace import skills_state

def list_marketplace_skills(
    db: Optional[Session] = None,
    *,
    include_disabled: bool = False,
    viewer_user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all market skill metadata: built-in default skills + preloaded (featured first, downloads descending) + community submissions.

    ``include_disabled``: the admin console passes True (sees everything +
    ``market_enabled`` annotation); the user side defaults to False (only sees
    listed items, and scoped items invisible to ``viewer_user_id`` are filtered
    by visibility). See ``marketplace_listing`` for the delist switch and
    visibility scopes.
    """
    items: List[Dict[str, Any]] = []
    if skills_state.MARKETPLACE_DIR.is_dir():
        for child in sorted(skills_state.MARKETPLACE_DIR.iterdir()):
            if not child.is_dir():
                continue
            m = skill_metadata.read_manifest(child.name)
            if m:
                items.append(skill_metadata.public_meta(m))
    items.sort(key=lambda x: (0 if x["featured"] else 1, -x["downloads"], x["slug"]))
    # Built-in default skills come first (built-in resident, shown with priority).
    items = skill_metadata.default_skill_metas() + items
    if db is not None:
        items.extend(skill_metadata.submission_public_meta(s) for s in skill_metadata.approved_submissions(db))
        from features.marketplace import edition as ml
        items = ml.annotate_and_filter(
            db, ml.KIND_SKILL, items, id_key="slug",
            include_disabled=include_disabled, viewer_user_id=viewer_user_id,
        )
    return items


def set_marketplace_skill_enabled(
    db: Session, slug: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    """List/delist a market skill (controls whether it shows in the skill marketplace; installed instances are unaffected)."""
    from features.marketplace import edition as ml
    return ml.set_listing_enabled(db, ml.KIND_SKILL, slug, enabled, updated_by=updated_by)


def list_categories(
    db: Optional[Session] = None,
    *,
    include_disabled: bool = False,
    viewer_user_id: Optional[str] = None,
) -> List[str]:
    """Marketplace category list: the 8 fixed categories first (stable order), legacy leftover categories appended after."""
    seen: List[str] = list(skills_state.MARKETPLACE_CATEGORIES)
    for it in list_marketplace_skills(
        db, include_disabled=include_disabled, viewer_user_id=viewer_user_id
    ):
        if it["category"] not in seen:
            seen.append(it["category"])
    return seen


def get_marketplace_skill(slug: str, db: Optional[Session] = None) -> Dict[str, Any]:
    """Single market skill detail: metadata + file list + SKILL.md body (for the detail preview).

    The preloaded directory takes priority; when not found, fall back to the
    community listing (approved submission).
    """
    # Built-in default skills: read extensions/skills/default/<slug>; the body comes from SKILL.md, the file list from the directory.
    if slug in skills_state.DEFAULT_SKILL_MARKET:
        meta = skill_metadata.default_skill_public_meta(slug)
        if meta is None:
            raise ResourceNotFoundError("marketplace_skill", slug)
        pkg_dir = skills_state.DEFAULT_SKILLS_DIR / slug
        files: List[Dict[str, Any]] = []
        for p in sorted(pkg_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(pkg_dir).as_posix()
            if rel == skills_state.SKILL_MD_NAME:
                continue
            files.append({"path": rel, "size": p.stat().st_size})
        skill_md = pkg_dir / skills_state.SKILL_MD_NAME
        meta["files"] = files
        meta["instructions"] = package_installation.strip_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore")) if skill_md.is_file() else ""
        return meta
    m = skill_metadata.read_manifest(slug)
    if not m:
        sub = skill_metadata.get_approved_submission(db, slug) if db is not None else None
        if sub is None:
            raise ResourceNotFoundError("marketplace_skill", slug)
        data = skill_metadata.submission_public_meta(sub)
        data["files"] = [
            {"path": rel, "size": len(str(content).encode("utf-8"))}
            for rel, content in sorted((sub.extra_files or {}).items())
            if rel != skills_state.REQUIRED_SECRETS_SNAPSHOT  # internal sentinel, not part of the installed content
        ]
        data["instructions"] = package_installation.strip_frontmatter(sub.skill_content or "")
        return data
    pkg_dir = skills_state.MARKETPLACE_DIR / slug
    data = skill_metadata.public_meta(m)

    files: List[Dict[str, Any]] = []
    for p in sorted(pkg_dir.rglob("*")):
        if not p.is_file() or p.name == skills_state.MANIFEST_NAME:
            continue
        rel = p.relative_to(pkg_dir).as_posix()
        if rel == skills_state.SKILL_MD_NAME:  # SKILL.md is returned separately as instructions; don't duplicate it in the file list
            continue
        files.append({"path": rel, "size": p.stat().st_size})
    body = ""
    skill_md = pkg_dir / skills_state.SKILL_MD_NAME
    if skill_md.is_file():
        body = package_installation.strip_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
    data["files"] = files
    data["instructions"] = body
    return data


def is_installed(db: Session, slug: str, owner_user_id: Optional[str]) -> bool:
    """Whether this market skill has been installed by the given scope (global / current user)."""
    if slug in skills_state.DEFAULT_SKILL_MARKET:
        return True  # built-in default skills are globally resident; considered always "installed"
    m = skill_metadata.read_manifest(slug)
    if not m:
        if skill_metadata.get_approved_submission(db, slug) is None:
            return False
        entry_name = slug
    else:
        entry_name = m["entry_name"]
    install_id = package_installation.compute_install_id(entry_name, owner_user_id)
    q = db.query(AdminSkill).filter(AdminSkill.skill_id == install_id)
    if owner_user_id is None:
        q = q.filter(AdminSkill.owner_user_id.is_(None))
    else:
        q = q.filter(AdminSkill.owner_user_id == owner_user_id)
    return q.first() is not None


def annotate_installed(
    items: List[Dict[str, Any]], db: Session, owner_user_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Annotate each market list item with an ``installed`` flag (for the given scope). One batched query decides all."""
    if not items:
        return items
    install_ids = {
        it["slug"]: package_installation.compute_install_id(it["entry_name"], owner_user_id) for it in items
    }
    q = db.query(AdminSkill.skill_id, AdminSkill.dep_status)
    if owner_user_id is None:
        q = q.filter(AdminSkill.owner_user_id.is_(None))
    else:
        q = q.filter(AdminSkill.owner_user_id == owner_user_id)
    q = q.filter(AdminSkill.skill_id.in_(list(install_ids.values())))
    status_map = {sid: dep for sid, dep in q.all()}
    # Rejected skills carry the admin's reason, surfaced to the user ("Rejected by admin: ...").
    rejected_ids = [sid for sid, dep in status_map.items() if dep == "rejected"]
    reason_map: Dict[str, Optional[str]] = {}
    if rejected_ids:
        from core.capabilities.skills.deps.requests import get_reject_reason
        for sid in rejected_ids:
            reason_map[sid] = get_reject_reason(db, sid)
    for it in items:
        # Built-in default skills are globally resident and always available to
        # everyone — mark them "installed/ready" directly (the frontend button
        # shows "already built-in"); no AdminSkill lookup, no install flow.
        if it.get("builtin"):
            it["installed"] = True
            it["dep_status"] = "ready"
            it["dep_reason"] = None
            continue
        iid = install_ids[it["slug"]]
        it["installed"] = iid in status_map
        # dep_status: 'installing' = dependencies installing / 'rejected' = admin declined / 'ready' = usable. None when not installed.
        it["dep_status"] = status_map.get(iid) if iid in status_map else None
        it["dep_reason"] = reason_map.get(iid)
    return items
