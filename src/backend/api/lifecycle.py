"""Application lifecycle and worker ownership."""
from api.startup.commands import startup_commands, shutdown_commands
import os
from contextlib import asynccontextmanager
from typing import Callable
from fastapi import FastAPI
from core.infra.logging import get_logger
logger = get_logger(__name__)
from api.startup.workers import (
    _env_flag,
    _startup_preload_enabled,
    _shutdown_automation_scheduler,
    _shutdown_idle_session_reaper,
    _startup_idle_session_reaper,
    _startup_preload,
    _startup_automation_scheduler,
    _startup_kb_wiki_worker,
    _startup_kb_index_worker,
    _shutdown_kb_index_worker,
    _shutdown_kb_wiki_worker,
    _startup_channel_manager,
    _startup_distillation_scheduler,
    _shutdown_pools,
)
from api.startup.bootstrap import (
    _startup_ensure_tables,
    _startup_seed_ce_admin,
    _startup_prepare_artifact_records,
    _startup_seed_page_config,
    _startup_seed_prompt_versions,
    _startup_seed_roles,
)
from api.startup.runtime import (
    _startup_thread_budget,
    _startup_watch_capability_changes,
    _startup_recover_chat_runs,
    _startup_resume_loops,
    _startup_recover_jobs,
    _startup_orphan_job_reaper,
    _startup_stale_run_reaper,
    _shutdown_stale_run_reaper,
    _shutdown_orphan_job_reaper,
    _shutdown_channel_manager,
    _startup_warm_sandbox_pool,
    _shutdown_sandbox_maintenance,
    _shutdown_sandbox_provider,
    _startup_local_sidecars,
    _shutdown_local_sidecars,
    _startup_device_link,
    _shutdown_device_link,
    _startup_reconcile_datasource_catalog,
    _startup_recover_datasource_sidecars,
    _shutdown_datasource_sidecar_recovery,
    _startup_desktop_observability,
    _shutdown_desktop_observability,
    _startup_channel_desktop,
    _shutdown_channel_desktop,
    _startup_extensions,
    _shutdown_extensions,
)
from api.startup.plugins import (
    _startup_seed_mcp_servers,
    _startup_mcp_market_monitor,
    _shutdown_mcp_market_monitor,
    _startup_upgrade_sites_plugin,
    _startup_plugin_device_assets,
    _startup_migrate_local_capabilities,
    _startup_backfill_tool_schemas,
    _startup_backfill_api_protocol,
    _startup_seed_default_plugins,
)
from api.startup.memory import (
    _startup_evolution_scheduler,
    _startup_memory_ttl_scheduler,
    _startup_memory_outbox_worker,
    _shutdown_memory_outbox_worker,
    _startup_persona_distill_worker,
    _shutdown_persona_distill_worker,
    _startup_warmup_memory,
)

SERVICE = "service"
EXECUTION_PLANE = "execution_plane"
_ALL_ROLES = frozenset({SERVICE, EXECUTION_PLANE})
_SERVICE_ONLY = frozenset({SERVICE})
_EXECUTION_PLANE_ONLY = frozenset({EXECUTION_PLANE})


def _runtime_role() -> str:
    from features.desktop.auth.bridge import bridge_enabled

    return EXECUTION_PLANE if bridge_enabled() else SERVICE


# Where a startup step belongs when the backend runs several uvicorn workers.
#
# ``_PER_WORKER`` is for work that either belongs to this process (in-process
# caches, capability watchers, observability drains) **or already claims each
# unit of work it takes**. The second kind matters more than it looks: the KB
# index and wiki workers and the memory outbox each claim a row before working
# it, and the daily schedulers take a day lock, so they are built to be run by
# every worker and get faster for it. Marking those singleton would put a
# coarser lock on top of a finer one and collapse a queue that N workers could
# drain back down to one.
#
# ``_SINGLETON`` is for work with no such per-unit claim: outbound channel
# connections, host sidecars, sweep loops over shared rows, one-shot recovery
# passes and the seeding of default rows. There a second copy is not slower,
# it is wrong — two seeders race on the same unique key, two recovery passes
# fight over the same orphan rows. Gate-phase singletons run behind a boot
# barrier so no worker starts serving against half-seeded state; deferred ones
# run only in the worker currently holding the leader lease.
_PER_WORKER = "per_worker"
_SINGLETON = "singleton"


def _startup_steps():
    """Every startup step, with what stops it and where it belongs.

    ``stop`` is the handler that shuts the step down, or ``None`` for the
    one-shot steps that leave nothing running. Keeping it in the row rather
    than in a separate list is what stops the two from drifting: a singleton
    added later cannot be started by the leader and then forgotten when that
    leader loses the role.
    """
    return (
        # (step, stop, gate, roles, scope)
        (_startup_thread_budget, None, True, _ALL_ROLES, _PER_WORKER),
        (_startup_ensure_tables, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_watch_capability_changes, None, True, _ALL_ROLES, _PER_WORKER),
        (_startup_desktop_observability, None, True, _ALL_ROLES, _PER_WORKER),
        (_startup_seed_ce_admin, None, True, _ALL_ROLES, _SINGLETON),
        # One-time and idempotent: every worker rewriting the artifact index
        # would duplicate the work and race on the same records.
        (_startup_prepare_artifact_records, None, False, _ALL_ROLES, _SINGLETON),
        (_startup_seed_page_config, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_seed_prompt_versions, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_seed_roles, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_seed_mcp_servers, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_seed_default_plugins, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_upgrade_sites_plugin, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_migrate_local_capabilities, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_plugin_device_assets, None, False, _ALL_ROLES, _SINGLETON),
        # Runs after the plugin seeding/upgrade steps above, which rewrite manifests
        # and are exactly what can leave a server with display-only tool entries.
        (_startup_backfill_tool_schemas, None, False, _ALL_ROLES, _SINGLETON),
        (_startup_backfill_api_protocol, None, False, _ALL_ROLES, _SINGLETON),
        (_startup_reconcile_datasource_catalog, None, True, _ALL_ROLES, _SINGLETON),
        (_startup_local_sidecars, _shutdown_local_sidecars, True, _ALL_ROLES, _SINGLETON),
        (_startup_recover_chat_runs, None, True, _ALL_ROLES, _SINGLETON),
        (startup_commands, shutdown_commands, True, _ALL_ROLES, _PER_WORKER),
        (_startup_device_link, _shutdown_device_link, False, _EXECUTION_PLANE_ONLY, _SINGLETON),
        (_startup_extensions, _shutdown_extensions, False, _SERVICE_ONLY, _PER_WORKER),
        (_startup_resume_loops, None, False, _ALL_ROLES, _SINGLETON),
        (_startup_recover_jobs, None, False, _ALL_ROLES, _SINGLETON),
        (_startup_orphan_job_reaper, _shutdown_orphan_job_reaper, False, _ALL_ROLES, _SINGLETON),
        (_startup_stale_run_reaper, _shutdown_stale_run_reaper, False, _ALL_ROLES, _SINGLETON),
        (_startup_warm_sandbox_pool, _shutdown_sandbox_maintenance, False, _ALL_ROLES, _SINGLETON),
        (
            _startup_idle_session_reaper,
            _shutdown_idle_session_reaper,
            False,
            _ALL_ROLES,
            _SINGLETON,
        ),
        (
            _startup_mcp_market_monitor,
            _shutdown_mcp_market_monitor,
            False,
            _SERVICE_ONLY,
            _SINGLETON,
        ),
        (
            _startup_recover_datasource_sidecars,
            _shutdown_datasource_sidecar_recovery,
            False,
            _SERVICE_ONLY,
            _SINGLETON,
        ),
        (_startup_preload, None, False, _ALL_ROLES, _PER_WORKER),
        (
            _startup_automation_scheduler,
            _shutdown_automation_scheduler,
            False,
            _ALL_ROLES,
            _SINGLETON,
        ),
        # Singleton, not per-worker: one wiki ingest job may spend thousands of
        # LLM calls behind two inner thread pools and batches five jobs at a
        # time, so a second copy doubles both its resident set and the gateway
        # quota it takes from live chats — and two of them on one KB lose
        # writes (see claim_next_job). This is the only cross-process guarantee
        # there is; claim_next_job only narrows the window.
        (_startup_kb_wiki_worker, _shutdown_kb_wiki_worker, False, _SERVICE_ONLY, _SINGLETON),
        # The next five claim each unit of work before doing it — a queued
        # document, an outbox row, a day — so every worker may run them and the
        # queue-shaped ones drain faster for it. See ``_PER_WORKER`` above.
        (_startup_kb_index_worker, _shutdown_kb_index_worker, False, _SERVICE_ONLY, _PER_WORKER),
        (_startup_distillation_scheduler, None, False, _SERVICE_ONLY, _PER_WORKER),
        (_startup_evolution_scheduler, None, False, _SERVICE_ONLY, _PER_WORKER),
        (_startup_memory_ttl_scheduler, None, False, _ALL_ROLES, _PER_WORKER),
        (
            _startup_memory_outbox_worker,
            _shutdown_memory_outbox_worker,
            False,
            _ALL_ROLES,
            _PER_WORKER,
        ),
        (_startup_persona_distill_worker, _shutdown_persona_distill_worker, False, _SERVICE_ONLY, _PER_WORKER),
        (_startup_warmup_memory, None, False, _ALL_ROLES, _PER_WORKER),
        (_startup_channel_manager, _shutdown_channel_manager, False, _SERVICE_ONLY, _SINGLETON),
        (_startup_channel_desktop, _shutdown_channel_desktop, False, _ALL_ROLES, _SINGLETON),
    )


async def _stop_singleton_work() -> None:
    """Stop the singleton loops this worker started while it held the lease.

    Only reached when a worker that *was* the leader failed to renew for a
    whole lease — it stalled for tens of seconds and another worker has taken
    the role. The handlers come from the registry, so this cannot stop a
    different set than the one the leader started.
    """
    failures = []
    for _step, stop, _gate, _roles, scope in reversed(_startup_steps()):
        if scope is not _SINGLETON or stop is None:
            continue
        try:
            await stop()
        except Exception as exc:  # noqa: BLE001 - one failure must not skip the rest
            logger.warning("leader_stop_failed", step=stop.__name__, error=str(exc))
            failures.append(exc)
    if failures:
        raise ExceptionGroup("Singleton work did not stop", failures)


async def _run_startup_steps(steps) -> None:
    """依次跑这些启动步骤。

    出错要喊出来：这些步骤跑在一个没人 await 的任务里，异常默默逃走之后，后面的步骤
    也一起不跑了，而日志里一个字都没有 —— 曾经因此让一个必须常驻的后台 worker 没起来，
    却看不出任何异常。喊完照旧中断：半跑起来的启动比不跑更难排查。
    """
    for step in steps:
        try:
            await step()
        except Exception:
            logger.exception("startup_step_failed", step=step.__name__)
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    import contextlib
    import time

    from core.infra.leader import Leadership, coordination_required, run_once

    role = _runtime_role()
    selected = [
        (step, gate, scope) for step, _stop, gate, roles, scope in _startup_steps() if role in roles
    ]
    started = time.monotonic()
    # Gate phase: a singleton step runs in one worker while the others wait for
    # it, so nobody begins serving against half-seeded state.
    for step, gate, scope in selected:
        if not gate:
            continue
        if scope is _SINGLETON:
            await run_once(step.__name__, step)
        else:
            await step()
    deferred = [(step, scope) for step, gate, scope in selected if not gate]
    logger.info(
        "startup_gate_ready",
        role=role,
        workers=os.getenv("WEB_CONCURRENCY", "1"),
        seconds=round(time.monotonic() - started, 2),
        deferred_steps=len(deferred),
    )
    app.state.deferred_startup = asyncio.create_task(
        _run_startup_steps([step for step, scope in deferred if scope is _PER_WORKER])
    )

    # Deferred singletons belong to whichever worker holds the lease. With one
    # worker that is decided without touching any store, so this path is what
    # it always was; with several, exactly one gets them, and if that worker
    # dies the lease lapses and the next one picks them up.
    singleton_steps = [step for step, scope in deferred if scope is _SINGLETON]
    leadership = Leadership("backend")
    leadership.start(
        on_elected=lambda: _run_startup_steps(singleton_steps),
        on_deposed=_stop_singleton_work,
    )
    if coordination_required():
        logger.info("startup_singleton_steps_gated", steps=len(singleton_steps))
    yield
    # ── shutdown ──
    await leadership.stop()
    deferred = getattr(app.state, "deferred_startup", None)
    if deferred is not None and not deferred.done():
        deferred.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deferred
    await shutdown_commands()
    await _shutdown_extensions()
    await _shutdown_stale_run_reaper()
    await _shutdown_memory_outbox_worker()
    await _shutdown_desktop_observability()
    from features.desktop.gateway.observability import drain

    await drain()
    await _shutdown_orphan_job_reaper()
    await _shutdown_kb_wiki_worker()
    await _shutdown_kb_index_worker()
    await _shutdown_channel_desktop()
    await _shutdown_channel_manager()
    await _shutdown_datasource_sidecar_recovery()
    await _shutdown_mcp_market_monitor()
    await _shutdown_local_sidecars()
    await _shutdown_sandbox_provider()
    await _shutdown_pools()
