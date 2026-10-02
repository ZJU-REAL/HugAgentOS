"""Agent assembly phase: runtime listeners. """

from __future__ import annotations

from core.llm.factory.request import AgentRequest
from core.llm.factory.runtime import evidence as factory_evidence_helpers


async def runtime_listeners(
    request: AgentRequest,
    *,
    _bind_manifest,
    _elapsed,
    _log,
    _manifest_for_surface,
    _plugin_runtime,
    agent,
    http_clients,
    profile,
    skill_selection,
    system_prompt,
    toolkit,
    transient_mcp_clients,
):
    async def _publish_context_manifest(request_manifest):  # noqa: ANN001, ANN202
        """Bind the exact post-budget context manifest used for this request."""
        try:
            from core.evolution.runtime_binding import rebind_execution_manifest

            request_bundle = rebind_execution_manifest(
                run_id=request.run_id,
                capability_scope=request.capability_scope,
                base_bundle=getattr(agent, "_jx_asset_bundle", None),
                execution_manifest=request_manifest,
            )
            agent.bind_request_evidence(request_manifest, bundle=request_bundle)
            _log.info(
                "[manifest] request aggregate=%s context_manifest=%s",
                request_manifest.aggregate_hash,
                request_manifest.context_manifest_hash,
            )
        except Exception as exc:  # evidence refresh must not fail the turn
            _log.warning("[manifest] request context binding unavailable: %s", exc)
            # Keep the in-memory request manifest even if durable evidence is
            # unavailable, but remove the stale base bundle. Episode assembly
            # treats a missing bundle as partial instead of falsely persisting
            # the pre-context surface as complete request evidence.
            agent.clear_request_evidence(request_manifest)
            from core.evolution.runtime_binding import clear_run_binding

            clear_run_binding(request.run_id, capability_scope=request.capability_scope)

    agent.set_context_manifest_listener(_publish_context_manifest)

    async def _publish_surface_generation(surface):  # noqa: ANN001, ANN202
        """Refresh run evidence before AgentScope consumes a changed surface."""
        # Progressive load_plugin changes the actual prompt/tool surface during
        # a run. Keep post-turn compaction on the latest generation even when
        # evidence binding itself is temporarily unavailable.
        factory_evidence_helpers.cache_compaction_execution_surface(agent, system_prompt, surface)
        try:
            next_manifest = _manifest_for_surface(surface)
            next_bundle = _bind_manifest(next_manifest)
            agent.bind_execution_surface(next_manifest, bundle=next_bundle)
            _log.info(
                "[manifest] generation=%s aggregate=%s",
                next_manifest.surface_generation,
                next_manifest.aggregate_hash,
            )
        except Exception as exc:  # evidence refresh must not fail the turn
            _log.warning("[manifest] surface generation refresh unavailable: %s", exc)
            try:
                agent.bind_execution_surface(None, bundle=_bind_manifest(None))
            except Exception:  # pragma: no cover - last-resort availability path
                pass

    toolkit.set_execution_surface_listener(_publish_surface_generation)
    if skill_selection is not None:
        try:
            setattr(agent, "_jx_skill_selection", skill_selection)
        except Exception:  # pragma: no cover
            pass
    # Which assembly governed this run, for the turn card. Carried rather than
    # re-resolved after the response: by then a profile may have been published
    # or switched off, and the card would name one the run never used.
    try:
        setattr(
            agent,
            "_jx_profile",
            {
                "profile_id": profile.profile_id,
                "version": profile.version,
                "task_types": list(profile.task_types),
                "narrowed_tools": profile.tool_allowlist is not None,
            },
        )
    except Exception:  # pragma: no cover
        pass

    _log.info("[factory] +%s agent created, TOTAL setup done", _elapsed())

    # Only transient (per-request) stdio clients + HTTP clients get closed;
    # pooled stable clients stay open for reuse (closing them would defeat the
    # pool and hand dead clients to the next request).
    all_transient = [*transient_mcp_clients, *http_clients]
    # Clients connected by a mid-run load_plugin activation are appended to this
    # same list object, so the caller's close_clients() teardown covers them.
    if _plugin_runtime is not None:
        _plugin_runtime["close_list"] = all_transient
    return agent, all_transient
