"""skill publishing responsibilities."""
from __future__ import annotations
from core.capabilities.skills import zip_reader
import uuid
from typing import Any, Dict, List, Optional, Tuple
from core.capabilities.skills.registry import load_skill_metadata_from_str
from features.marketplace.models import MarketplaceSubmission
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.time import utc_now
from core.ontology.build_validator import ensure_ontology_build_valid
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
import logging
logger = logging.getLogger(__name__)
from features.marketplace import skill_archives
from features.marketplace import skill_metadata
from features.marketplace import skill_submission_data
from features.marketplace import skills_state

def upsert_admin_submission(
    db: Session,
    *,
    skill_id: str,
    skill_content: str,
    display_name: str,
    summary: str,
    category: str,
    tags: List[str],
    version: str,
    extra_files: Dict[str, str],
    note: str,
    submitter_name: str,
    user_intro: Optional[str] = None,
) -> Tuple[str, str]:
    """Upsert an approved listing record under the ADMIN_UPLOAD_OWNER sentinel, returning (slug, action).

    When the same skill_id already has an admin listing record, update it
    (keeping the original slug); otherwise create one. Shared by the two admin
    listing entry points: "upload zip" and "create via form".
    """
    category = skill_metadata.validate_category(category)
    snapshot_files = dict(extra_files)
    # Credential files are never distributed with the market snapshot (same as community listing).
    snapshot_files.pop(skills_state.SECRETS_FILENAME, None)
    now = utc_now()
    existing = (
        db.query(MarketplaceSubmission)
        .filter(
            MarketplaceSubmission.skill_id == skill_id,
            MarketplaceSubmission.owner_user_id == skills_state.ADMIN_UPLOAD_OWNER,
        )
        .first()
    )
    if existing is not None:
        existing.display_name = display_name
        existing.summary = summary
        existing.user_intro = str(user_intro or "").strip() or None
        existing.category = category
        existing.tags = list(tags or [])
        existing.version = version or "1.0.0"
        existing.skill_content = skill_content
        existing.extra_files = snapshot_files
        existing.note = note
        existing.status = "approved"
        existing.review_note = ""
        existing.reviewed_at = now
        existing.updated_at = now
        flag_modified(existing, "tags")
        flag_modified(existing, "extra_files")
        return existing.slug, "updated"

    slug = skill_submission_data.derive_slug(db, skill_id, skills_state.ADMIN_UPLOAD_OWNER)
    db.add(MarketplaceSubmission(
        submission_id=f"mkadm_{uuid.uuid4().hex[:16]}",
        slug=slug,
        skill_id=skill_id,
        owner_user_id=skills_state.ADMIN_UPLOAD_OWNER,
        submitter_name=(submitter_name or "").strip(),
        display_name=display_name,
        summary=summary,
        user_intro=str(user_intro or "").strip() or None,
        category=category,
        tags=list(tags or []),
        version=version or "1.0.0",
        note=note,
        skill_content=skill_content,
        extra_files=snapshot_files,
        status="approved",
        reviewed_at=now,
        created_at=now,
        updated_at=now,
    ))
    return slug, "published"


def publish_skill_zip_to_marketplace(
    db: Session,
    data: bytes,
    *,
    category: str,
    display_name: str = "",
    summary: str = "",
    submitter_name: str = "管理员上传",
) -> Dict[str, Any]:
    """Admin uploads a skill zip → listed directly in the marketplace as approved (not in the catalog, not globally effective).

    When the same skill (by SKILL.md's name = skill_id) is uploaded again, the
    existing admin listing record is updated (keeping the original slug) rather
    than adding a new one each time.
    """
    parsed = zip_reader.parse_skill_zip(data)
    meta = parsed["meta"]
    name = (display_name or "").strip() or meta.name or parsed["skill_id"]
    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=name,
        description=(summary or "").strip() or meta.description or "",
        instructions=parsed["skill_content"],
        tool_names=list(meta.allowed_tools or []),
        ontology_tags=list(meta.tags or []),
    )
    slug, action = upsert_admin_submission(
        db,
        skill_id=parsed["skill_id"],
        skill_content=parsed["skill_content"],
        display_name=name,
        summary=(summary or "").strip() or meta.description or "",
        category=category,
        tags=list(meta.tags or []),
        version=meta.version or "1.0.0",
        extra_files=parsed["extra_files"],
        note="管理员上传，免审核直接上架",
        submitter_name=submitter_name,
    )
    db.commit()
    logger.info("marketplace_skill_admin_%s: skill=%s slug=%s", action, parsed["skill_id"], slug)
    return {
        "slug": slug,
        "skill_id": parsed["skill_id"],
        "display_name": name,
        "category": skill_metadata.validate_category(category),
        "stored_files": len([k for k in parsed["extra_files"] if k != skills_state.SECRETS_FILENAME]),
        "skipped": parsed["skipped"],
        "action": action,
        "message": "技能已上架技能市场" if action == "published" else "技能市场内容已更新",
    }


def publish_skill_to_marketplace(
    db: Session,
    *,
    skill_id: str,
    skill_content: str,
    display_name: str,
    description: str,
    version: str,
    tags: List[str],
    category: str,
    user_intro: Optional[str] = None,
    submitter_name: str = "管理员新建",
) -> Dict[str, Any]:
    """Admin "create skill" form → listed in the marketplace as approved (not in the catalog, not globally effective).

    ``skill_content`` is the complete SKILL.md assembled from the form fields
    (valid frontmatter).
    """
    name = (display_name or "").strip() or skill_id
    try:
        meta = load_skill_metadata_from_str(skill_content, skill_id)
        allowed_tools = list(meta.allowed_tools or [])
        parsed_description = meta.description or ""
    except Exception:  # Some reviewed legacy drafts have minimal frontmatter.
        allowed_tools = []
        parsed_description = ""
    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=name,
        description=description or parsed_description,
        instructions=skill_content,
        tool_names=allowed_tools,
        ontology_tags=list(tags or []),
    )
    slug, action = upsert_admin_submission(
        db,
        skill_id=skill_id,
        skill_content=skill_content,
        display_name=name,
        summary=(description or "").strip(),
        category=category,
        tags=list(tags or []),
        version=version or "1.0.0",
        extra_files={},
        note="管理员新建，免审核直接上架",
        submitter_name=submitter_name,
        user_intro=user_intro,
    )
    db.commit()
    logger.info("marketplace_skill_admin_%s: skill=%s slug=%s (form)", action, skill_id, slug)
    return {
        "slug": slug,
        "skill_id": skill_id,
        "display_name": name,
        "category": skill_metadata.validate_category(category),
        "action": action,
        "message": "技能已上架技能市场" if action == "published" else "技能市场内容已更新",
    }


def delete_marketplace_skill(db: Session, slug: str) -> Dict[str, Any]:
    """Admin deletes a DB listing record from the skill marketplace (admin upload / user community listing).

    Preloaded (filesystem) skills ship with the repo and cannot be deleted
    online — straight 400. Deletion only removes it from the market; it does not
    affect any installed skill instances (installs are content snapshots).
    """
    if skill_metadata.read_manifest(slug) is not None:
        raise BadRequestError(message="预置技能随仓库发布，不可在线删除")
    sub = (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.slug == slug)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("marketplace_skill", slug)
    skill_id, owner = sub.skill_id, sub.owner_user_id
    db.delete(sub)
    db.commit()
    logger.info("marketplace_skill_deleted: slug=%s skill=%s owner=%s", slug, skill_id, owner)
    return {"slug": slug, "deleted": True}
