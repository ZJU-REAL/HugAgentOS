"""Resolve a user's ontology preference into a runtime policy and asset tags."""
from __future__ import annotations
from core.ontology import service as ontology_service
from typing import Any
from core.infra.exceptions import ServiceUnavailableError
from core.ontology.runtime_pack import register_runtime_asset_tags
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)

def disabled_ontology_runtime() -> dict[str, Any]:
    """Return the canonical pass-through policy used when the user opts out."""
    return {"enabled": False, "packs": [], "review_level": "none"}


def build_ontology_runtime_for_preference(
    *,
    enabled: bool,
    task: str,
    db: Session,
    pack_ids: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Resolve the effective ontology switch and compile its runtime policy.

    A user's stored opt-in is subordinate to the administrator-controlled pack
    availability.  Disabling the selected/default packs is therefore an
    intentional global off switch, not a runtime-policy failure.  Once an
    active pack exists, compilation remains fail-closed so malformed policy
    data cannot silently bypass validation.
    """
    if not enabled:
        return False, disabled_ontology_runtime()

    service = ontology_service.OntologyService(db)
    selected = pack_ids or None
    if not service.repo.get_active_versions(selected):
        return False, disabled_ontology_runtime()

    runtime = service.build_runtime(task=task, pack_ids=selected)
    if not runtime.get("enabled"):
        raise ServiceUnavailableError("本体校验运行时策略编译失败")
    return True, runtime


def runtime_needs_asset_tag_lookup(
    runtime: dict[str, Any],
    *,
    kind: str,
    asset_id: str,
) -> bool:
    """Return whether tags could activate an otherwise-unmatched workflow."""
    activated = set(runtime.get("activated_workflows", []))
    for candidate in runtime.get("activation_candidates", []):
        ref = f"{candidate.get('pack_id')}:{candidate.get('workflow_id')}"
        if ref in activated:
            continue
        triggers = [
            trigger
            for trigger in candidate.get("asset_triggers", [])
            if trigger.get("kind") == kind
        ]
        if not triggers:
            continue
        if any(asset_id in set(trigger.get("ids", [])) for trigger in triggers):
            continue
        if any(trigger.get("tags_any") for trigger in triggers):
            return True
    return False


def resolve_runtime_asset_tags(
    *,
    runtime: dict[str, Any],
    kind: str,
    asset_id: str,
    user_id: str,
) -> list[str]:
    """Resolve and request-cache one invoked asset's trusted ontology tags.

    The runtime catalog doubles as a per-turn positive/negative cache. Metadata
    failures are fail-closed so an asset cannot execute without the governance
    policy that its server-authoritative tags may activate.
    """
    if not runtime.get("enabled") or kind not in {"tool", "skill", "subagent"} or not asset_id:
        return []
    catalog = runtime.setdefault("asset_tags", {}).setdefault(kind, {})
    if asset_id in catalog:
        return list(catalog[asset_id])
    if not runtime_needs_asset_tag_lookup(runtime, kind=kind, asset_id=asset_id):
        register_runtime_asset_tags(runtime, kind=kind, asset_id=asset_id, tags=[])
        return []

    try:
        from core.db.engine import SessionLocal

        with SessionLocal() as db:
            tags = ontology_service.OntologyService(db).resolve_asset_tags(
                kind=kind,
                asset_id=asset_id,
                user_id=user_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ontology] lazy asset tag lookup failed kind=%s asset_id=%s",
            kind,
            asset_id,
            exc_info=True,
        )
        raise ServiceUnavailableError("本体资产标签读取失败，本次资产调用已停止") from exc

    register_runtime_asset_tags(runtime, kind=kind, asset_id=asset_id, tags=tags)
    return list(runtime["asset_tags"][kind][asset_id])


def build_user_ontology_runtime(
    *,
    user_id: str,
    task: str,
    db: Session | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Resolve one user's opt-in and compile the task-scoped runtime policy.

    Web chat, channels, automation, batch, plan mode, and autonomous loops all
    use this resolver so independent execution paths cannot silently bypass the
    setting selected by the user.
    """
    from core.auth.users import UserService

    owns_session = db is None
    if owns_session:
        from core.db.engine import SessionLocal

        db = SessionLocal()
    assert db is not None
    try:
        from core.ontology.policy import user_can_use_ontology_validation

        if not user_can_use_ontology_validation(db, user_id):
            return False, disabled_ontology_runtime()
        settings = UserService(db).get_user_settings(user_id)
        opted_in = bool(settings.get("ontology_enabled", False))
        if not opted_in:
            return False, disabled_ontology_runtime()
        pack_ids = settings.get("ontology_pack_ids")
        selected = (
            [str(item) for item in pack_ids if str(item).strip()]
            if isinstance(pack_ids, list)
            else None
        )
        return build_ontology_runtime_for_preference(
            enabled=opted_in,
            task=task,
            db=db,
            pack_ids=selected or None,
        )
    finally:
        if owns_session:
            db.close()
