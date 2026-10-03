"""Agent assembly: capabilities. """

from __future__ import annotations

from typing import Optional

import core.agent_skills.loader as skill_loader
import core.config.catalog as catalog


def _device_available_skill_ids(
    skill_ids: list[str], *, plugin_ids: list[str], user_id: Optional[str]
) -> list[str]:
    """把一批绑定技能收敛到本设备真正可用的集合。

    在桌面能力层启用时（双端 / 本机模式），一个智能体可能绑定了本账号在此设备上并未拥有
    的云端技能（如别的插件/范围里的技能）。云端才是能力真源：先按需准备这批技能里属于当前
    账号的云端条目，再只保留解析器能选出的（已就绪、无冲突）名字。拿不到的绑定技能被丢弃，
    智能体用可用技能照常运行，而不是整轮硬失败。非能力层部署原样返回。
    """
    if not skill_ids:
        return skill_ids
    import logging as _logging

    _log = _logging.getLogger(__name__)
    from core.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return skill_ids
    from core.capabilities import skills as _caps_skills
    from core.capabilities.preparation import ensure_cloud_ready

    try:
        ensure_cloud_ready(user_id, skill_keys=skill_ids, plugin_keys=plugin_ids)
        available = set(
            _caps_skills.filter_available_names(
                skill_ids, user_id=_caps_skills.current_local_user_id()
            )
        )
    except Exception as exc:  # noqa: BLE001 — 解析失败时不放大成整轮失败，退回原始集合
        _log.warning("[factory] device skill availability filter failed: %s", exc)
        return skill_ids
    dropped = [sid for sid in skill_ids if sid not in available]
    if dropped:
        _log.info("[factory] agent bound skills unavailable on this device, skipped: %s", dropped)
    return [sid for sid in skill_ids if sid in available]


def _filter_skill_ids_for_user(skill_ids: list[str], user_id: Optional[str]) -> list[str]:
    """Strip out skill ids this user must not load.

    Two independent rules, applied at the one choke point every agent (main,
    sub-agent, batch) passes through:

    1. **Ownership.** Kept: public skills (``owner_user_id`` empty, including
       filesystem/built-in skills absent from ``admin_skills``) + the user's own
       private skills. Dropped: other users' private skills.
    2. **Release exposure.** An evolution-authored skill is only loadable once
       its release actually reaches this user — ``active``, or ``canary`` with the
       user in the bucket. A ``shadow`` release reaches nobody. Without this the
       release ladder was decorative: a materialised skill was a global public
       row, so a ``risk_tier=high`` capability the system wrote about itself went
       to every user at once.
    """
    if not skill_ids:
        return skill_ids
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill
        from core.evolution.exposure import filter_skill_ids as filter_evolved

        with SessionLocal() as db:
            owned = dict(
                db.query(AdminSkill.skill_id, AdminSkill.owner_user_id)
                .filter(
                    AdminSkill.skill_id.in_(skill_ids),
                    AdminSkill.owner_user_id.isnot(None),
                )
                .all()
            )
            allowed = [sid for sid in skill_ids if owned.get(sid) in (None, user_id)]
            return filter_evolved(allowed, user_id=user_id or "", db=db)
    except Exception:
        # Ownership filtering degrades to "keep what was asked for", as before.
        # Exposure does not: an evolved skill whose release state we could not
        # read is withheld, because the unsafe direction here is loading an
        # unvetted self-authored capability.
        try:
            from core.evolution.exposure import is_evolved_skill_id

            return [sid for sid in skill_ids if not is_evolved_skill_id(sid)]
        except Exception:
            return skill_ids


def _filter_kb_ids_for_user(kb_ids: list[str], user_id: Optional[str]) -> list[str]:
    """Strip out KB ids the current user has no access to (local + external collections), preventing unauthorized ids passed in from the frontend.

    Single source of truth ``core.auth.kb_permissions``: public KBs are visible
    to everyone, private KBs to their owner, and scoped-visibility KBs per
    grant. On failure, fall back to returning the input unchanged (this doesn't
    escalate permissions — it only avoids hurting availability, and the
    downstream retrieve's authorization intercepts again).
    """
    if not kb_ids or not user_id:
        return kb_ids
    try:
        from core.auth.kb_permissions import filter_accessible_kb_ids
        from core.db.engine import SessionLocal

        with SessionLocal() as db:
            return filter_accessible_kb_ids(db, str(user_id), kb_ids)
    except Exception:
        return kb_ids


def _expand_plugin_bindings(
    plugin_ids: list[str], *, user_id: Optional[str] = None
) -> tuple[list[str], list[str]]:
    """Expand bound plugin install_ids into (skill id list, MCP server id list).

    Takes each plugin's bundled skills / mcp from
    ``InstalledPlugin.component_ids``. Returns empty on failure — best-effort,
    never blocks agent construction.
    """
    if not plugin_ids:
        return [], []
    skills: list[str] = []
    mcp: list[str] = []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import InstalledPlugin

        with SessionLocal() as db:
            rows = (
                db.query(InstalledPlugin).filter(InstalledPlugin.install_id.in_(plugin_ids)).all()
            )
            for r in rows:
                cids = r.component_ids or {}
                from core.plugins.packaging.sources import _component_keys

                skills.extend(_component_keys(cids, "skills"))
                mcp.extend(_component_keys(cids, "mcp"))
    except Exception:  # noqa: BLE001
        skills, mcp = [], []
    from core.capabilities.paths import capabilities_enabled

    if capabilities_enabled():
        from core.capabilities.plugins import cloud_binding_ids

        cloud_skills, cloud_mcp = cloud_binding_ids(plugin_ids, user_id=user_id)
        skills.extend(cloud_skills)
        mcp.extend(cloud_mcp)
    return list(dict.fromkeys(skills)), list(dict.fromkeys(mcp))


def _effective_main_available_skills() -> list[str]:
    """Resolve main-agent skills from currently enabled catalog skills."""

    def available(names):
        from core.capabilities.paths import capabilities_enabled

        if capabilities_enabled():
            from core.capabilities import skills

            return skills.filter_available_names(names, user_id=skills.current_local_user_id())
        return names

    enabled_ids = [
        sid for sid in catalog.get_enabled_ids("skills") if isinstance(sid, str) and sid.strip()
    ]
    if enabled_ids:
        return available(enabled_ids)

    # On the server, an empty default catalog means no ambient skills. The
    # loader also knows installed-but-disabled skills for explicit selection.
    from core.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return []

    try:
        loader = skill_loader.get_skill_loader()
        discovered = sorted(loader.load_all_metadata().keys())
        if discovered:
            return available(discovered)
    except Exception:
        pass

    return []


def _mcp_ids_bound_to_skills(skill_ids: list[str]) -> list[str]:
    """Collect explicit MCP bindings declared by enabled skills."""
    if not skill_ids:
        return []
    try:
        metadata = skill_loader.get_skill_loader().load_all_metadata()
    except Exception:
        return []
    result: list[str] = []
    for skill_id in skill_ids:
        item = metadata.get(skill_id)
        if item is None:
            continue
        for server_id in item.mcp_server_ids or []:
            if server_id and server_id not in result:
                result.append(server_id)
    return result
