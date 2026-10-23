"""Version-aware sources for the existing skill marketplace API."""

from __future__ import annotations
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session
from core.capabilities.skills.binary_files import is_binary_value
from core.capabilities.skills.cache_refresh import refresh_skill_caches
from core.capabilities.skills.deps.detector import detect_dependencies
from core.capabilities.skills.registry import load_skill_metadata_from_str
from core.db.models import AdminSkill
from core.infra.exceptions import BadRequestError
from core.infra.time import utc_now
from core.ontology.build_validator import ensure_ontology_build_valid
from fastapi import HTTPException
import features.marketplace.skill_catalog as market_owner_skill_catalog
from features.marketplace.skill_installation import build_user_intro as _build_user_intro
import features.marketplace.skill_installation as market_owner_skill_installation
from features.marketplace.skills_state import DEFAULT_SKILL_MARKET
from features.marketplace.skills_state import MANIFEST_NAME
import features.marketplace.skills_state as market_owner_skills_state
from sqlalchemy.orm.attributes import flag_modified
import logging
logger = logging.getLogger(__name__)


import features.marketplace.versions.store as store
from core.capabilities.skills.installation import inject_secrets as _inject_secrets, rewrite_frontmatter_name as _rewrite_frontmatter_name, compute_install_id as compute_install_id






def get_marketplace_skill(slug, db=None):
    native = market_owner_skill_catalog.get_marketplace_skill(slug, db)
    return store.detail(db, "skill", slug, native)


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
    current = market_owner_skill_catalog.get_marketplace_skill(slug, db)
    snapshot = store.active(db, "skill", slug)
    if not snapshot:
        return market_owner_skill_installation.install_marketplace_skill(
            db, slug, owner_user_id=owner_user_id, secrets=secrets
        )
    if current.get("builtin"):
        raise BadRequestError(message="内置技能已全局可用，无需安装")
    secrets = secrets or {}
    from core.capabilities.skills.binary_files import encode_upload

    files = store.decode(snapshot)
    m = store.detail(db, "skill", slug, current)
    skill_content = files["SKILL.md"].decode()
    extra_files = {
        n: encode_upload(n, v)
        for n, v in files.items()
        if n not in {"SKILL.md", MANIFEST_NAME, market_owner_skills_state.REQUIRED_SECRETS_SNAPSHOT}
    }

    entry_name = str(m["entry_name"]).strip()
    install_id = compute_install_id(entry_name, owner_user_id)
    skill_content = _rewrite_frontmatter_name(skill_content, install_id)

    required_secrets = list(m.get("required_secrets") or [])
    if required_secrets:
        skill_content = _inject_secrets(skill_content, extra_files, required_secrets, secrets)

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
    user_intro = _build_user_intro(m) or None

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
        if existing.owner_user_id != owner_user_id:
            if existing.owner_user_id is None:
                raise HTTPException(
                    status_code=409, detail=f"技能 id 「{install_id}」与公共技能冲突"
                )
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
        db.add(
            AdminSkill(
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
            )
        )
        action = "installed"
    db.commit()
    try:
        from core.capabilities.skills.icons import get_skill_icon, preset_for_category, set_skill_icon

        if not get_skill_icon(db, install_id):
            set_skill_icon(db, install_id, preset_for_category(m.get("category")))
    except Exception as exc:  # noqa: BLE001
        logger.debug("marketplace set default icon failed: %s", exc)
    refresh_skill_caches()
    logger.info(
        "marketplace_skill_%s: slug=%s id=%s owner=%s files=%d",
        action,
        slug,
        install_id,
        owner_user_id or "global",
        len(extra_files),
    )
    return {
        "id": install_id,
        "slug": slug,
        "owner": "self" if owner_user_id else "global",
        "action": action,
        "dependencies": dependencies,
        "dep_pending": False,
        "message": "技能已安装" if action == "installed" else "技能已更新",
    }
