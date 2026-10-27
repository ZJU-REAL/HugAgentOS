"""Frozen per-run project allowlist, applied before every capability assembly."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from threading import Lock

from core.projects.environment.config import FIELDS
from core.projects.environment.service import ProjectEnvironmentService
from fastapi import HTTPException

# Compare-and-store only; resource lookups never run under this lock.


def _snapshot(context, key, value):
    with process_state().snapshot_lock:
        if context.get(key) is None:
            context[key] = value
        return context[key]


@dataclass(frozen=True)
class ProjectEnvironmentPolicy:
    agent_ids: frozenset[str]
    plugin_ids: frozenset[str]
    skill_ids: frozenset[str]
    mcp_ids: frozenset[str]
    custom: bool = True

    def check_invocation(self, request):
        checks = (
            (self.skill_ids, [request.required_skill_id, *(request.invoked_skill_ids or [])]),
            (self.mcp_ids, [*(request.required_mcp_ids or []), *(request.invoked_mcp_ids or [])]),
            (self.plugin_ids, [request.required_plugin_id]),
            (self.agent_ids, [getattr(request.user_agent, "agent_id", None)]),
        )
        for allowed, requested in checks:
            if any(ident and ident not in allowed for ident in requested):
                raise HTTPException(403, "所选能力未绑定到此项目，或当前用户没有使用权限")

    def restrict(self, request, *, resolve_defaults=True):
        from core.capabilities.catalog.api import get_enabled_ids
        from core.llm.factory.selection.capabilities import effective_main_available_skills as _effective_main_available_skills

        if resolve_defaults and request.enabled_skill_ids is None:
            request.enabled_skill_ids = _effective_main_available_skills()
        if resolve_defaults and request.enabled_mcp_ids is None:
            request.enabled_mcp_ids = list(get_enabled_ids("mcp"))
        if request.enabled_skill_ids is not None:
            request.enabled_skill_ids = [
                sid for sid in request.enabled_skill_ids if sid in self.skill_ids
            ]
        if request.enabled_mcp_ids is not None:
            request.enabled_mcp_ids = [
                sid for sid in request.enabled_mcp_ids if sid in self.mcp_ids
            ]
        request.visible_subagents = [
            item for item in (request.visible_subagents or []) if item["agent_id"] in self.agent_ids
        ]

    def restrict_sticky(self, sticky):
        for field, allowed in (
            ("plugin_ids", self.plugin_ids),
            ("plugin_skill_ids", self.skill_ids),
            ("direct_skill_ids", self.skill_ids),
            ("plugin_mcp_ids", self.mcp_ids),
            ("direct_mcp_ids", self.mcp_ids),
        ):
            setattr(sticky, field, [ident for ident in getattr(sticky, field) if ident in allowed])

    def restrict_plugins(self, resolution, request):
        if resolution is None:
            return None
        old_skills, old_mcps = resolution.deferred_skill_ids, resolution.deferred_mcp_ids
        for field in ("directory", "deferred"):
            entries = [p for p in getattr(resolution, field) if p.install_id in self.plugin_ids]
            for plugin in entries:
                plugin.skill_ids = [s for s in plugin.skill_ids if s in self.skill_ids]
                plugin.mcp_ids = [m for m in plugin.mcp_ids if m in self.mcp_ids]
                plugin.bound_mcp_ids = [m for m in plugin.bound_mcp_ids if m in self.mcp_ids]
            setattr(resolution, field, entries)
        from core.capabilities.plugins.runtime.resolution import finalize_resolution as _finalize_resolution

        _finalize_resolution(resolution, resolution.directory)
        # A disallowed plugin sharing an allowed loose skill must not consume
        # that skill's registration through progressive deferral.
        request.enabled_skill_ids = list(
            dict.fromkeys(
                [*(request.enabled_skill_ids or []), *(old_skills - resolution.deferred_skill_ids)]
            )
        )
        request.enabled_mcp_ids = list(
            dict.fromkeys(
                [*(request.enabled_mcp_ids or []), *(old_mcps - resolution.deferred_mcp_ids)]
            )
        )
        self.restrict(request)
        return resolution if resolution.directory else None


def _resolve(request):
    from core.auth.edition import resolve_project_permission
    from core.db.engine import SessionLocal
    from core.db.models import ChatSession, Project
    from core.projects.environment.dependencies import expand_environment
    from core.projects.environment_resources import environment_resources

    context = request.project_ctx if request.project_ctx is not None else {}
    project_id = context.get("project_id")
    with SessionLocal() as db:
        # Covers workers whose callers only know the persistent chat id.
        chat = db.get(ChatSession, request.chat_id) if request.chat_id else None
        if chat and chat.project_id:
            if project_id and project_id != chat.project_id:
                raise HTTPException(403, "对话与项目不匹配")
            project_id = chat.project_id
        if not project_id:
            return None
        project = db.get(Project, project_id)
        if not project or project.deleted_at or not request.current_user_id:
            raise HTTPException(403, "项目不可用")
        level = resolve_project_permission(db, request.current_user_id, project)
        if level == "none":
            raise HTTPException(403, "没有项目访问权限")
        context["project_id"] = project_id
        # This field is populated exclusively by server-side project context
        # builders. Keeping it in the parent context freezes settings for child
        # runs even when the project is edited while a response is executing.
        config = _snapshot(
            context, "project_environment_config", ProjectEnvironmentService(db).read(project)
        )
        graph = environment_resources(db, request.current_user_id)
        frozen = context.get("project_environment_effective")
        if frozen is None:
            # setdefault gives concurrent plan steps the first completed
            # snapshot, not independently expanded dependency graphs.
            frozen = _snapshot(
                context,
                "project_environment_effective",
                expand_environment(config, graph)["effective"],
            )
        # Revoked ownership/admin permissions may narrow a frozen run. They
        # can never widen it. A project-settings edit alone keeps this snapshot.
        effective = {field: frozenset(frozen[field]) & frozenset(graph[field]) for field in FIELDS}
    request.project_ctx = context
    return ProjectEnvironmentPolicy(**effective, custom=config["mode"] == "custom")


async def bind_project_environment(request):
    policy = await asyncio.to_thread(_resolve, request)
    request.project_environment = policy
    if policy is not None:
        policy.check_invocation(request)
        if (
            policy.custom
            and request.user_agent is None
            and not request.isolated
        ):
            request.enabled_skill_ids = sorted(policy.skill_ids)
            request.enabled_mcp_ids = sorted(policy.mcp_ids)
        # Assembly resolves ambient defaults after sticky preparation.
        policy.restrict(request, resolve_defaults=False)
    return policy


def validate_project_environment(request):
    """Current authorization may narrow a run; project setting edits cannot widen it."""
    if request.project_environment is None:
        return
    current = _resolve(request)
    if current != request.project_environment:
        removed = {
            name: sorted(getattr(request.project_environment, name) - getattr(current, name, frozenset()))
            for name in FIELDS
        }
        logging.getLogger(__name__).warning(
            "project_environment_revoked run_id=%s removed=%s project_available=%s",
            getattr(request, "run_id", None), removed, current is not None,
        )
        raise HTTPException(403, "项目资源权限已变更，本轮已暂停，请重新发起对话")


@dataclass
class ProjectEnvironmentState:
    snapshot_lock: object = field(default_factory=lambda: Lock())


_STATE = ProjectEnvironmentState()


def process_state() -> ProjectEnvironmentState:
    return _STATE
