"""Publish approved agent profiles and sub-agents."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from core.db.engine import SessionLocal
import logging
from core.evolution.cycle import activation_policy
from core.evolution.cycle import activation_types

logger = logging.getLogger(__name__)

def materialise_profile_candidate(
    candidate_id: str, *, approver: str, tenant_id: str = "default"
) -> Dict[str, Any]:
    """Publish an approved agent profile.

    Profiles do not ride the shadow → canary ladder that skills do, and the
    reason is not leniency. A profile *is* the assembly, so a canary would mean
    two different assemblies running concurrently under one profile id, with
    every downstream measurement of either one confounded by the other. Instead
    a profile is scoped: it governs one task type, and its blast radius is that
    task type's traffic, recorded on the release row like any other change.

    Rolling back is switching ``is_active`` off, which restores the built-in
    assembly on the next run — no restart, because profiles are read per run.
    """
    from core.db.models.evolution import EvolutionCandidate, EvolutionRelease

    from core.db.models.evolution import EvolutionAgentProfile
    from core.evolution.profiles.agent_profile import AgentProfile, validate_profile
    from core.evolution.profiles.agent_profile_constants import TARGET_KIND
    from core.evolution.cycle.release import ACTIVE as _ACTIVE, new_release_id

    if not approver:
        raise activation_types.ActivationError("approver_required", "激活必须署名")

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise activation_types.ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if candidate.target_kind != TARGET_KIND:
            raise activation_types.ActivationError(
                "unsupported_kind", f"该入口只处理编排候选，收到 {candidate.target_kind}"
            )
        if approver == (candidate.proposer or ""):
            raise activation_types.ActivationError("self_approval_forbidden", "生成者不能激活自己提出的候选")
        if activation_policy.is_frozen(db):
            raise activation_types.ActivationError("layer_frozen", "进化已被全局冻结")

        payload: Dict[str, Any] = {}
        for change in candidate.ir.get("changes") or []:
            if isinstance(change.get("profile"), dict):
                payload = change["profile"]
                break
        if not payload:
            raise activation_types.ActivationError("no_profile_payload", "候选未携带 profile 定义")

        profile = AgentProfile.from_dict(payload)
        profile.scope = {**(profile.scope or {}), "tenant_id": tenant_id or "default"}

        # Re-validated at activation against the *current* tool grant, not the
        # one that existed when the candidate was written. A grant narrowed in
        # the meantime must not be widened again by approving an older proposal.
        from core.capabilities.catalog.api import get_enabled_ids

        base_tools = [str(t) for t in get_enabled_ids("tools")] + [
            str(t) for t in get_enabled_ids("mcp")
        ]
        ok, problems = validate_profile(profile, base_tools=base_tools)
        if not ok:
            raise activation_types.ActivationError("profile_invalid", f"编排定义未通过校验: {problems}")

        previous = db.get(EvolutionAgentProfile, profile.profile_id)
        previous_payload = dict(previous.payload) if previous is not None else None

        # One active profile per (task type, scope). Two would make the
        # resolution order the thing that decides behaviour, which is not a
        # decision anyone reviewed.
        for row in (
            db.query(EvolutionAgentProfile)
            .filter(EvolutionAgentProfile.is_active.is_(True))
            .all()
        ):
            if row.profile_id == profile.profile_id:
                continue
            same_types = set(row.task_types or []) == set(profile.task_types)
            same_scope = dict(row.scope or {}) == dict(profile.scope)
            if same_types and same_scope:
                row.is_active = False

        row = previous or EvolutionAgentProfile(profile_id=profile.profile_id)
        row.version = profile.version
        row.task_types = list(profile.task_types)
        row.scope = dict(profile.scope)
        row.payload = profile.to_dict()
        row.candidate_id = candidate_id
        row.is_active = True
        row.created_by = f"evolution:{approver}"
        if previous is None:
            db.add(row)

        release = EvolutionRelease(
            release_id=new_release_id(),
            candidate_id=candidate_id,
            target_kind=TARGET_KIND,
            target_asset_id=profile.profile_id,
            stage=_ACTIVE,
            traffic_percent=100,
            scope_filter=dict(profile.scope),
            guardrails={"previous": previous_payload, "proposer": candidate.proposer or ""},
            rollback_version_id=(previous_payload or {}).get("version") or "builtin",
            approved_by=approver,
        )
        db.add(release)

        candidate.status = _ACTIVE
        candidate.approved_by = approver
        candidate.approved_at = datetime.now(timezone.utc)
        db.commit()

        # 血缘：哪些技能被装配进了这个 profile。
        try:
            from core.evolution.evidence import lineage

            lineage.record_links(
                [
                    (
                        "skill",
                        str(skill_id),
                        lineage.REL_ASSEMBLED_INTO,
                        "agent_profile",
                        f"{profile.profile_id}@{profile.version}",
                    )
                    for skill_id in profile.skill_ids
                ],
                candidate_id=candidate_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[activation] profile lineage skipped: %s", exc)

        logger.info(
            "[activation] profile %s active for %s (tools=%s skills=%s)",
            profile.profile_id,
            profile.task_types or ["*"],
            "narrowed" if profile.tool_allowlist is not None else "unchanged",
            len(profile.skill_ids),
        )
        return {
            "profile_id": profile.profile_id,
            "release_id": release.release_id,
            "task_types": list(profile.task_types),
            "scope": dict(profile.scope),
            "tool_allowlist": profile.tool_allowlist,
            "skill_ids": list(profile.skill_ids),
            "replaced_previous": previous_payload is not None,
        }


def materialise_subagent_candidate(
    candidate_id: str, *, approver: str, owner_user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Create an approved sub-agent, and nothing else.

    Deliberately does **not** install the route that sends work to it. The route
    is a sentence in the parent's prompt, and a sentence in the parent's prompt
    changes every request the parent handles — so it goes through prompt review
    as its own candidate. Creating the agent here and routing to it there means
    approving the agent cannot silently also approve a behaviour change.

    The agent is created disabled for the same reason: an agent nobody routes to
    does nothing, and one that is enabled before its route is reviewed is a
    capability sitting in the product that no reviewer has seen used.
    """
    from core.db.models.evolution import EvolutionCandidate, EvolutionRelease

    from core.db.models import UserAgent
    from core.evolution.cycle.release import SHADOW, new_release_id

    if not approver:
        raise activation_types.ActivationError("approver_required", "激活必须署名")

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise activation_types.ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if candidate.target_kind != "subagent":
            raise activation_types.ActivationError(
                "unsupported_kind", f"该入口只处理子智能体候选，收到 {candidate.target_kind}"
            )
        if approver == (candidate.proposer or ""):
            raise activation_types.ActivationError("self_approval_forbidden", "生成者不能激活自己提出的候选")
        if activation_policy.is_frozen(db):
            raise activation_types.ActivationError("layer_frozen", "进化已被全局冻结")

        spec: Dict[str, Any] = {}
        for change in candidate.ir.get("changes") or []:
            if change.get("agent_id"):
                spec = change
                break
        if not spec:
            raise activation_types.ActivationError("no_subagent_payload", "候选未携带子智能体定义")

        # The subset check runs again at activation against the current grant.
        from core.capabilities.catalog.api import get_enabled_ids

        base_tools = {str(t) for t in get_enabled_ids("tools")} | {
            str(t) for t in get_enabled_ids("mcp")
        }
        requested = {str(t) for t in (spec.get("tools_allowlist") or [])}
        widened = sorted(requested - base_tools)
        if widened:
            raise activation_types.ActivationError(
                "tool_escalation", f"子智能体工具集超出主体授权: {widened}"
            )
        if spec.get("may_delegate"):
            raise activation_types.ActivationError("subagent_nesting", "子智能体不得再派生子智能体")

        agent_id = str(spec.get("agent_id"))
        existing = db.get(UserAgent, agent_id)
        if existing is not None:
            raise activation_types.ActivationError("agent_exists", f"子智能体已存在: {agent_id}")

        agent = UserAgent(
            agent_id=agent_id,
            owner_type="user" if owner_user_id else "admin",
            user_id=owner_user_id,
            name=str(spec.get("name") or agent_id)[:255],
            description=str(spec.get("responsibility") or "")[:2000],
            system_prompt=str(spec.get("responsibility") or ""),
            skill_ids=[str(s) for s in (spec.get("required_skills") or [])],
            mcp_server_ids=sorted(requested),
            kb_ids=[],
            plugin_ids=[],
            max_iters=12,
            timeout=int(spec.get("timeout") or 180),
            # Disabled until its route is approved — see the docstring.
            is_enabled=False,
            extra_config={
                "origin": "evolution",
                "candidate_id": candidate_id,
                "route_hint": str(spec.get("route_hint") or ""),
                "task_types": [str(t) for t in (spec.get("task_types") or [])],
                "tools_allowlist": sorted(requested),
                "may_delegate": False,
            },
        )
        db.add(agent)

        release = EvolutionRelease(
            release_id=new_release_id(),
            candidate_id=candidate_id,
            target_kind="subagent",
            target_asset_id=agent_id,
            # Reaches nobody until the route exists, which is what shadow means.
            stage=SHADOW,
            traffic_percent=0,
            scope_filter={"owner_user_id": owner_user_id} if owner_user_id else {},
            guardrails={"proposer": candidate.proposer or "", "route_pending": True},
            rollback_version_id="v0",
            approved_by=approver,
        )
        db.add(release)

        candidate.approved_by = approver
        candidate.approved_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "[activation] subagent %s created (disabled, route pending) tools=%s",
            agent_id,
            sorted(requested),
        )
        return {
            "agent_id": agent_id,
            "release_id": release.release_id,
            "stage": SHADOW,
            "is_enabled": False,
            "tools_allowlist": sorted(requested),
            "route_hint": str(spec.get("route_hint") or ""),
            "next_step": "路由句需作为提示词候选单独审批后，子智能体才会被启用",
        }
