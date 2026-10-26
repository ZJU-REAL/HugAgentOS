"""Application lifecycle operations."""

import core.chat.runtime.recovery as recovery
import orchestration.loops.recovery as loops_recovery
import orchestration.workers.run_recovery as _run_recovery
from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

async def _startup_thread_budget():
    """Size this worker's thread pools to the connection pool before serving."""
    from core.db.threads import apply_thread_budget

    apply_thread_budget()


async def _startup_watch_capability_changes():
    """智能体 / 技能 / 连接器 / 插件被增删时递增能力变更号，桌面端据此同步一次。"""
    from features.desktop.capabilities.change_signal import watch_capability_tables
    from core.db.engine import engine

    watch_capability_tables(engine)

async def _startup_recover_chat_runs():
    """Resume claimable chat_runs; valid leases are retried by the recovery loop."""
    try:
        count = await recovery.recover_orphan_runs()
        if count:
            logger.info("[startup] chat_runs orphan recovered: %d", count)
    except Exception as exc:
        logger.warning("[startup] chat_runs orphan recovery failed: %s", exc)

async def _startup_resume_loops():
    """Resume interrupted autonomous loops (M4, off by default; enable with LOOP_AUTO_RESUME=true)."""
    try:
        count = await loops_recovery.resume_running_loops()
        if count:
            logger.info("[startup] autonomous loops resumed: %d", count)
    except Exception as exc:
        logger.warning("[startup] autonomous loop resume failed: %s", exc)

async def _startup_recover_jobs():
    """作业编排：进程重启后活跃 job 全是孤儿（进程内没有 driver task），归位 interrupted。

    与 chat_run 不同的是，job 的工作项台账在 DB —— 归位后 ``run_job action=resume``
    可直接断点续跑，已完成的项不会重做。
    """
    try:
        import core.jobs.runtime as job_runtime

        count = await job_runtime.resume_running_jobs()
        if count:
            logger.info("[startup] orphan jobs recovered: %d", count)
    except Exception as exc:
        logger.warning("[startup] job orphan recovery failed: %s", exc)

async def _startup_orphan_job_reaper():
    """周期性对账失联的批量作业（驱动没了就没人管，护栏跟着一起没）。"""
    from api.app import app
    try:
        import asyncio

        import core.jobs.runtime as job_runtime

        task = asyncio.create_task(job_runtime.run_job_reaper_loop())
        app.state.orphan_job_reaper_task = task
        logger.info("[startup] job orphan reaper started")
    except Exception as exc:
        logger.warning("[startup] job orphan reaper start failed: %s", exc)

async def _startup_stale_run_reaper():
    """Start periodic lease recovery plus the slower stale-run watchdog."""
    from api.app import app
    try:
        import asyncio

        pass

        task = asyncio.create_task(_run_recovery.run_stale_reaper_loop())
        app.state.stale_run_reaper_task = task
        logger.info("[startup] chat_run stale reaper started")
    except Exception as exc:
        logger.warning("[startup] chat_run stale reaper start failed: %s", exc)

async def _shutdown_stale_run_reaper():
    import asyncio
    import contextlib

    from api.app import app

    task = getattr(app.state, "stale_run_reaper_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

async def _shutdown_orphan_job_reaper():
    import asyncio
    import contextlib

    from api.app import app

    task = getattr(app.state, "orphan_job_reaper_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

async def _shutdown_channel_manager():
    try:
        from integrations.channels.manager import get_manager

        get_manager().shutdown()
    except Exception:  # noqa: BLE001
        pass

async def _startup_warm_sandbox_pool():
    """Provider-agnostic warmup hook: call provider.warmup() so providers that
    support prewarming (opensandbox) start background pool filling. Providers
    that don't have this method → no-op.
    """
    try:
        from core.sandbox import get_sandbox_provider

        provider = get_sandbox_provider()
        warmup = getattr(provider, "warmup", None)
        if warmup is None:
            return
        await warmup()
        logger.info("[startup] sandbox provider %s warmup kicked off", provider.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[startup] sandbox warmup failed: %s", exc)
        raise

async def _startup_local_sidecars():
    """Spawn and verify MCP + script_runner sidecars in local profile.

    A local desktop process must not pass its health check while the MCP tools
    promised by the default plugins are absent.  Compose deployments are a
    no-op inside ``start_local_sidecars`` and keep their existing best-effort
    startup behavior.
    """
    try:
        from core.infra.local_runtime.startup import start_local_sidecars

        await start_local_sidecars()
    except Exception as exc:
        logger.error("[startup] local sidecars failed readiness: %s", exc)
        if settings.deploy.is_local:
            raise

async def _shutdown_local_sidecars():
    """Reap local-profile sidecar subprocesses on shutdown."""
    try:
        from core.infra.local_runtime.startup import stop_local_sidecars

        await stop_local_sidecars()
    except Exception as exc:
        logger.warning("[shutdown] local sidecars stop failed: %s", exc)

async def _startup_device_link():
    """Serve this computer's Personal Agent mailbox (desktop execution plane only)."""
    from personal_agent.device_link.runner import runner

    runner.start()

async def _shutdown_device_link():
    from personal_agent.device_link.runner import runner

    await runner.stop()

async def _startup_reconcile_datasource_catalog():
    """Finish local capability reconciliation before the readiness gate opens."""
    from orchestration.workers.edition_startup import reconcile_datasource_catalog

    reconcile_datasource_catalog()


async def _startup_recover_datasource_sidecars():
    """Recover configured DBHub/ES MCP sidecars without blocking API startup."""
    import asyncio

    from api.app import app

    async def _recover() -> None:
        try:
            from orchestration.workers.edition_startup import recover_datasource_sidecars

            result = await recover_datasource_sidecars()
            failures = [
                state.get("error", "unknown error")
                for key in ("dbhub", "es")
                if (state := result.get(key) or {}).get("desired") and not state.get("ok")
            ]
            if failures:
                logger.warning(
                    "[startup] datasource sidecar recovery incomplete: %s",
                    "; ".join(failures),
                )
                return
            recovered = [
                key for key in ("dbhub", "es") if (state := result.get(key) or {}).get("desired")
            ]
            if recovered:
                logger.info(
                    "[startup] datasource sidecars ready: %s",
                    ", ".join(recovered),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[startup] datasource sidecar recovery failed: %s", exc)

    app.state.datasource_sidecar_recovery_task = asyncio.create_task(_recover())

async def _shutdown_datasource_sidecar_recovery():
    import asyncio
    import contextlib

    from api.app import app

    task = getattr(app.state, "datasource_sidecar_recovery_task", None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

_desktop_sync_worker = None

_desktop_capture_remove = None

async def _startup_desktop_observability():
    global _desktop_sync_worker, _desktop_capture_remove
    from features.desktop.auth.bridge import bridge_enabled

    if not bridge_enabled():
        return
    from core.db.engine import SessionLocal
    from features.desktop.bridge.identity import get_identity_state
    from features.desktop.observability.sync import DesktopSyncWorker, install_capture

    if _desktop_capture_remove is None:
        _desktop_capture_remove = install_capture(SessionLocal, get_identity_state)
    if _desktop_sync_worker is None:
        _desktop_sync_worker = DesktopSyncWorker(SessionLocal)
        _desktop_sync_worker.start()

async def _shutdown_desktop_observability():
    global _desktop_sync_worker, _desktop_capture_remove
    if _desktop_sync_worker is not None:
        await _desktop_sync_worker.stop()
        _desktop_sync_worker = None
    if _desktop_capture_remove is not None:
        _desktop_capture_remove()
        _desktop_capture_remove = None

_channel_desktop_worker = None

async def _startup_channel_desktop():
    global _channel_desktop_worker
    from features.desktop.auth.bridge import bridge_enabled
    from core.db.engine import SessionLocal

    if bridge_enabled():
        from integrations.channels.desktop_worker import ChannelDesktopWorker

        _channel_desktop_worker = ChannelDesktopWorker(SessionLocal)
    else:
        from integrations.channels.relay_dispatch import ChannelRelayReaper

        _channel_desktop_worker = ChannelRelayReaper(SessionLocal)
    _channel_desktop_worker.start()

async def _shutdown_channel_desktop():
    global _channel_desktop_worker
    if _channel_desktop_worker is not None:
        await _channel_desktop_worker.stop()
        _channel_desktop_worker = None

async def _startup_extensions():
    from core.harness.registry import lifecycle_hooks

    for start, _ in lifecycle_hooks():
        await start()

async def _shutdown_extensions():
    from core.harness.registry import lifecycle_hooks

    for _, stop in reversed(lifecycle_hooks()):
        await stop()


async def _shutdown_sandbox_maintenance():
    from core.sandbox import get_sandbox_provider
    stop = getattr(get_sandbox_provider(), "stop_maintenance", None)
    if stop is not None:
        await stop()


async def _shutdown_sandbox_provider():
    from core.sandbox import get_sandbox_provider
    shutdown = getattr(get_sandbox_provider(), "shutdown", None)
    if shutdown is not None:
        await shutdown()
