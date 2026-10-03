"""Resolve explicit skill, connector and plugin invocation selections."""

from typing import List

from api.schemas import ChatRequest
from core.infra.logging import get_logger
from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = get_logger(__name__)


def _resolve_explicit_capability_invocation(
    db: Session,
    request: ChatRequest,
    user_id: str,
) -> ChatRequest:
    """Validate and expand capabilities explicitly selected for this turn.

    Personal catalog switches only control default assembly. Explicit ``/``
    and ``+`` selections may therefore use an installed capability even when
    its enabled switch is off, but cannot cross ownership, missing-installation,
    or dependency-readiness boundaries.
    """
    from core.config.catalog_resolver import resolve_explicit_runtime_capabilities

    def _ids(raw) -> List[str]:
        return list(
            dict.fromkeys(
                str(item).strip() for item in (raw or []) if isinstance(item, str) and item.strip()
            )
        )

    plugin_skill_ids: List[str] = []
    plugin_mcp_ids: List[str] = []
    allowed_plugin_skills: List[str] = []
    allowed_plugin_mcps: List[str] = []
    plugin_name = request.plugin_name
    if request.plugin_id:
        from core.db.models import InstalledPlugin

        installed = (
            db.query(InstalledPlugin)
            .filter(InstalledPlugin.install_id == request.plugin_id)
            .first()
        )
        if installed is None:
            from core.capabilities.errors import CapabilityError
            from core.capabilities.invocation import cloud_plugin_selection
            from core.capabilities.paths import capabilities_enabled

            try:
                cloud_plugin = (
                    cloud_plugin_selection(request.plugin_id, user_id=user_id)
                    if capabilities_enabled()
                    else None
                )
            except CapabilityError as exc:
                raise HTTPException(
                    status_code=409, detail="所选插件尚不可用，请检查能力中心状态"
                ) from exc
            if cloud_plugin is None:
                raise HTTPException(status_code=403, detail="无法访问该插件，可能已卸载")
            request = request.model_copy(update={"plugin_id": cloud_plugin["install_id"]})
            plugin_skill_ids = cloud_plugin["skills"]
            plugin_mcp_ids = cloud_plugin["mcp"]
            plugin_name = cloud_plugin["name"]
        else:
            if installed.owner_user_id is not None and installed.owner_user_id != user_id:
                raise HTTPException(status_code=403, detail="无法访问该插件，可能已卸载")
            from core.plugins.packaging.sources import _component_keys

            component_ids = installed.component_ids or {}
            plugin_skill_ids = _component_keys(component_ids, "skills")
            plugin_mcp_ids = _component_keys(component_ids, "mcp")
            plugin_name = str(installed.name or installed.slug or request.plugin_name or "插件")

    # A market skill may be installed globally and privately under a
    # user-suffixed ID. When an API client selects the public entry name, bind
    # the current user's own installation first so the selected content and
    # authorization both refer to the same package.
    if request.skill_id:
        from core.db.models import AdminSkill
        from core.services.marketplace_service import compute_install_id

        private_id = compute_install_id(request.skill_id, user_id)
        private_row = (
            db.query(AdminSkill.skill_id)
            .filter(
                AdminSkill.skill_id == private_id,
                AdminSkill.owner_user_id == user_id,
            )
            .first()
        )
        if private_row is not None:
            request = request.model_copy(update={"skill_id": private_id})

    # Clients submit only stable selection IDs. Plugin component lists always
    # come from the authoritative server-side installation record.
    strict_skill_ids = _ids([request.skill_id])
    strict_mcp_ids = _ids([request.connector_id])
    allowed_skills, allowed_mcps, unavailable_skills, unavailable_mcps = (
        resolve_explicit_runtime_capabilities(
            db,
            user_id,
            skill_ids=strict_skill_ids,
            mcp_ids=strict_mcp_ids,
        )
    )
    if unavailable_skills or unavailable_mcps:
        logger.warning(
            "explicit capability denied user=%s skills=%s mcps=%s",
            user_id,
            unavailable_skills,
            unavailable_mcps,
        )
        from core.capabilities import registry, skills
        from core.capabilities.paths import LOCAL_PROFILE, capabilities_enabled

        # An owned desktop installation may be missing files or temporarily
        # disabled. Retain its selection for an explanatory answer, without
        # granting anything the resolver rejected. Foreign identities still fail.
        owned = set()
        if capabilities_enabled():
            profiles = [LOCAL_PROFILE]
            if skills.account_authorized_for(user_id):
                profiles.append(skills.current_account_profile())
            owned = {
                (row.kind, row.key)
                for row in registry.list_installations(profiles=profiles)
                if row.payload.get("owner_user_id") in (None, "", user_id)
            }
        rejected = [("skill", name) for name in unavailable_skills] + [
            ("mcp", name) for name in unavailable_mcps
        ]
        if any(item not in owned for item in rejected):
            raise HTTPException(status_code=403, detail="显式选择的能力不可用或无权访问")

    if request.plugin_id:
        (
            allowed_plugin_skills,
            allowed_plugin_mcps,
            unavailable_plugin_skills,
            unavailable_plugin_mcps,
        ) = resolve_explicit_runtime_capabilities(
            db,
            user_id,
            skill_ids=plugin_skill_ids,
            mcp_ids=plugin_mcp_ids,
        )
        if unavailable_plugin_skills or unavailable_plugin_mcps:
            logger.info(
                "explicit plugin partially available plugin=%s skipped_skills=%s skipped_mcps=%s",
                request.plugin_id,
                unavailable_plugin_skills,
                unavailable_plugin_mcps,
            )
        # Keep the explicit selection so the model can explain unavailability.
        # No component grant is added when the installation has no usable tools.
        allowed_skills = _ids([*allowed_skills, *allowed_plugin_skills])
        allowed_mcps = _ids([*allowed_mcps, *allowed_plugin_mcps])

    single_skill_id = str(request.skill_id or "").strip()
    expanded_skill_ids = [sid for sid in allowed_skills if sid != single_skill_id]
    resolved = request.model_copy(update={"plugin_name": plugin_name})
    resolved._resolved_skill_ids = expanded_skill_ids
    resolved._resolved_mcp_ids = allowed_mcps
    resolved._resolved_plugin_skill_ids = allowed_plugin_skills
    resolved._resolved_plugin_mcp_ids = allowed_plugin_mcps
    return resolved
