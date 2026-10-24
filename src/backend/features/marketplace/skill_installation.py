"""skill installation responsibilities."""
from __future__ import annotations
from core.capabilities.skills import installation as package_installation
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from core.capabilities.skills.binary_files import is_binary_value, pack_directory
from core.capabilities.skills.cache_refresh import refresh_skill_caches
from core.capabilities.skills.deps.detector import detect_dependencies
from core.capabilities.skills.registry import load_skill_metadata_from_str
from core.db.models import AdminSkill
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.time import utc_now
from core.ontology.build_validator import ensure_ontology_build_valid
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
import logging
logger = logging.getLogger(__name__)
from features.marketplace import skill_metadata
from features.marketplace import skills_state

def load_package_files(pkg_dir: Path) -> Tuple[str, Dict[str, str]]:
    """Read a market skill package: returns (SKILL.md original text, {relative path: stored content}).

    Binary files are stored as base64 (consistent with admin upload), text as
    original text; the manifest and junk files are skipped, and per-file/total
    size caps are enforced.
    """
    skill_md = pkg_dir / skills_state.SKILL_MD_NAME
    if not skill_md.is_file():
        raise BadRequestError(message="市场技能包缺少 SKILL.md")
    skill_content = skill_md.read_text(encoding="utf-8")
    extra_files = pack_directory(pkg_dir, skip_names={skills_state.MANIFEST_NAME, skills_state.SKILL_MD_NAME})
    return skill_content, extra_files


def install_marketplace_skill(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    secrets: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Install a market skill as an ``AdminSkill`` (admin = global / user = private), returning a result dict.

    The preloaded directory takes priority; when the slug isn't in the preloaded
    directory, fall back to the community listing (approved submission), with
    install content taken from the submission-time snapshot.
    """
    # Built-in default skills are globally resident and always available to
    # everyone — no install needed (the frontend button is already disabled as
    # "already built-in"); this is a defensive block to avoid duplicating 1MB+
    # office skill content into the DB as a private copy.
    if slug in skills_state.DEFAULT_SKILL_MARKET:
        raise BadRequestError(message="内置技能已全局可用，无需安装")
    secrets = secrets or {}
    m = skill_metadata.read_manifest(slug)
    if m:
        pkg_dir = skills_state.MARKETPLACE_DIR / slug
        skill_content, extra_files = load_package_files(pkg_dir)
    else:
        sub = skill_metadata.get_approved_submission(db, slug)
        if sub is None:
            raise ResourceNotFoundError("marketplace_skill", slug)
        m = {
            "slug": sub.slug,
            "entry_name": sub.slug,
            "display_name": sub.display_name,
            "summary": sub.summary or "",
            "user_intro": sub.user_intro or "",
            "category": sub.category or "社区共享",
            "tags": list(sub.tags or []),
            "version": sub.version or "1.0.0",
            "required_secrets": skill_metadata.submission_required_secrets(sub),
        }
        skill_content = sub.skill_content or ""
        extra_files = dict(sub.extra_files or {})
        # The sentinel entry only carries the credential field names; it never lands in the install directory
        extra_files.pop(skills_state.REQUIRED_SECRETS_SNAPSHOT, None)

    entry_name = str(m["entry_name"]).strip()
    install_id = package_installation.compute_install_id(entry_name, owner_user_id)
    # Always rewrite the frontmatter ``name`` to the install id: this both
    # namespaces private user installs and, along the way, normalizes illegal
    # names in market skills (containing spaces/Chinese etc.) — otherwise
    # load_skill_metadata_from_str fails validation with "invalid skill id".
    # install_id comes from slug/entry_name and is always a legal slug.
    skill_content = package_installation.rewrite_frontmatter_name(skill_content, install_id)

    required_secrets = list(m.get("required_secrets") or [])
    if required_secrets:
        skill_content = package_installation.inject_secrets(
            skill_content, extra_files, required_secrets, secrets
        )

    # Validate SKILL.md (description required, etc.); an invalid one is a straight 400 rather than being persisted.
    try:
        meta = load_skill_metadata_from_str(skill_content, install_id)
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(message=f"市场技能 SKILL.md 不合法：{exc}")

    dependencies = detect_dependencies(
        {fn: c for fn, c in extra_files.items() if not is_binary_value(c)}
    )

    display_name = m.get("display_name") or meta.name or install_id
    description = meta.description or m.get("summary") or ""
    tags = list(m.get("tags") or meta.tags or [])
    version = m.get("version") or meta.version or "1.0.0"
    user_intro = build_user_intro(m) or None

    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=display_name or install_id,
        description=description,
        instructions=skill_content,
        tool_names=list(meta.allowed_tools or []),
        ontology_tags=list(tags),
    )

    now = utc_now()
    existing = db.query(AdminSkill).filter(AdminSkill.skill_id == install_id).first()
    if existing is not None:
        # Owner conflict: cannot overwrite a public skill or another user's private skill (admin reinstalling a global one is allowed)
        if existing.owner_user_id != owner_user_id:
            if existing.owner_user_id is None:
                raise HTTPException(status_code=409, detail=f"技能 id 「{install_id}」与公共技能冲突")
            raise HTTPException(status_code=409, detail=f"技能 id 「{install_id}」已被占用")
        existing.skill_content = skill_content
        existing.display_name = display_name
        existing.description = description
        existing.user_intro = user_intro
        existing.version = version
        existing.tags = tags
        existing.allowed_tools = list(meta.allowed_tools or [])
        existing.extra_files = extra_files
        existing.dependencies = dependencies
        existing.is_enabled = True
        existing.updated_at = now
        flag_modified(existing, "tags")
        flag_modified(existing, "extra_files")
        flag_modified(existing, "dependencies")
        action = "updated"
    else:
        db.add(AdminSkill(
            skill_id=install_id,
            skill_content=skill_content,
            display_name=display_name,
            description=description,
            user_intro=user_intro,
            version=version,
            tags=tags,
            allowed_tools=list(meta.allowed_tools or []),
            extra_files=extra_files,
            dependencies=dependencies,
            is_enabled=True,
            owner_user_id=owner_user_id,
            created_at=now,
            updated_at=now,
        ))
        action = "installed"
    db.commit()
    # Strict dependency verdicts happen at the async route layer (gate_skill_deps
    # needs to actually run probes in the sandbox; this function is synchronous).
    # Here we only carry the declared dependencies out with the result; the route
    # awaits the probe → decides installing/ready.
    # Assign the installed skill a built-in icon by category (users/admins can change it later in skill editing).
    try:
        from core.capabilities.skills.icons import get_skill_icon, preset_for_category, set_skill_icon
        if not get_skill_icon(db, install_id):
            set_skill_icon(db, install_id, preset_for_category(m.get("category")))
    except Exception as exc:  # noqa: BLE001
        logger.debug("marketplace set default icon failed: %s", exc)
    refresh_skill_caches()
    logger.info(
        "marketplace_skill_%s: slug=%s id=%s owner=%s files=%d",
        action, slug, install_id, owner_user_id or "global", len(extra_files),
    )
    return {
        "id": install_id,
        "slug": slug,
        "owner": "self" if owner_user_id else "global",
        "action": action,
        "dependencies": dependencies,
        # dep_pending / message are filled in by the route layer after awaiting gate_skill_deps (strict probe result).
        "dep_pending": False,
        "message": "技能已安装" if action == "installed" else "技能已更新",
    }


def build_user_intro(m: Dict[str, Any]) -> str:
    """Return only an explicitly authored user intro from marketplace metadata.

    The marketplace ``summary`` is card copy. Treating it as ``user_intro``
    suppresses the catalog's SKILL.md-body fallback and reduces rich skill
    documentation to a single line after installation.
    """
    return str(m.get("user_intro") or "").strip()
