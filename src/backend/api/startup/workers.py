"""Application lifecycle operations."""
import os
import sys
from typing import Callable

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

def _startup_preload_enabled() -> bool:
    return _env_flag("STARTUP_PRELOAD_ENABLED", True)

async def _shutdown_automation_scheduler():
    """Stop the automation scheduler, whether the process is exiting or this
    worker just lost the leader role. Idempotent: the scheduler's own ``stop``
    is safe to call on an already-stopped scheduler."""
    global _automation_scheduler
    if _automation_scheduler is not None:
        await _automation_scheduler.stop()

async def _shutdown_idle_session_reaper():
    import asyncio
    import contextlib

    global _idle_reaper_task
    if _idle_reaper_task is not None:
        _idle_reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _idle_reaper_task
        _idle_reaper_task = None

async def _startup_idle_session_reaper():
    """Background periodic reaping of idle persistent sandbox sessions (only opensandbox has reap_idle_sessions).

    With file/sandbox tools enabled in all modes, every ordinary conversation
    may bind a chat-level persistent container; idle ones are reclaimed per
    SANDBOX_IDLE_TTL_S.
    Fire-and-forget, does not block startup; canceled by _shutdown_pools on
    process exit.
    """
    import asyncio

    try:
        from core.sandbox import get_sandbox_provider

        provider = get_sandbox_provider()
        reap = getattr(provider, "reap_idle_sessions", None)
        if reap is None:
            return

        async def _run():
            while True:
                await asyncio.sleep(120)
                try:
                    n = await reap()
                    if n:
                        logger.info("[idle-reaper] reaped %d idle sandbox session(s)", n)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("[idle-reaper] reap cycle failed: %s", e)

        global _idle_reaper_task
        _idle_reaper_task = asyncio.create_task(_run())
        from core.infra import runtime_state

        runtime_state.register("idle_session_reaper", _idle_reaper_task)
        logger.info("[startup] idle sandbox-session reaper started")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[startup] idle session reaper failed to start: %s", exc)

async def _startup_preload():
    """Pre-load application caches at startup."""
    import asyncio

    if not _startup_preload_enabled():
        logger.info("[startup] Preload disabled for current environment")
        return

    async def _run():
        import time

        start = time.monotonic()

        async def _run_sync(label: str, func: Callable[[], None]) -> None:
            try:
                await asyncio.to_thread(func)
                logger.info("[startup] %s loaded", label)
            except Exception as exc:
                logger.warning("[startup] %s preload failed: %s", label, exc)

        # 1. Pre-load prompt config (mtime-cached, ~0ms after first call)
        from prompts.prompt_config import load_prompt_config

        await _run_sync("Prompt config", load_prompt_config)

        # 2. Pre-load skill metadata
        def _load_skill_metadata() -> None:
            from core.capabilities.skills.loader_factory import get_skill_loader

            loader = get_skill_loader()
            meta = loader.load_all_metadata()
            logger.info("[startup] Skill metadata loaded: %d skills", len(meta))

        await _run_sync("Skill metadata", _load_skill_metadata)

        # 2.5 Sync built-in skills into the unified sandbox skills dir so the
        #     single /workspace/skills bind mount exposes built-in + DB skills
        #     at the same in-sandbox path. DB skills materialize into the same
        #     dir on demand. Must run before sandboxes are created/used.
        def _sync_builtin_skills() -> None:
            from core.capabilities.skills.config import sync_builtin_skills_to_sandbox_dir
            from core.harness.registry.run_capabilities import has_run_capabilities

            if has_run_capabilities():
                # Desktop store: move the pre-store layout aside first (idempotent,
                # quarantines instead of deleting), then the views are link-only.
                from features.desktop.capabilities.migration import migrate_legacy_layout

                report = migrate_legacy_layout()
                if report is not None and (report.changed or report.skipped):
                    logger.info("[startup] Capability layout migration: %s", report.to_dict())

            n = sync_builtin_skills_to_sandbox_dir()
            logger.info("[startup] Built-in skills synced to sandbox dir: %d", n)
            # Pre-create the per-user skills root for the same reason as above:
            # script-runner bind-mounts it, and a dockerd-created one is root-owned.
            try:
                from core.capabilities.skills.config import get_user_skills_root

                get_user_skills_root()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[startup] pre-create user skills root failed: %s", exc)
            # The startup sync already reconciles from authoritative sources
            # under the publication lock; never prune using an earlier DB snapshot.
            # Pre-create the Yida workspace for the script-runner shared sandbox
            # (0777: the runner is uid 1001 and needs write, backend is 1000).
            # If the directory doesn't exist, the compose volume mount gets
            # created by dockerd as root and the runner can't write into it →
            # backend must create it first. opensandbox/cube don't use this one.
            try:
                from integrations.yida.storage import yida_shared_workspace_dir

                p = yida_shared_workspace_dir()
                p.mkdir(parents=True, exist_ok=True)
                p.chmod(0o777)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[startup] pre-create yida shared workspace failed: %s", exc)

        await _run_sync("Built-in skills sandbox sync", _sync_builtin_skills)

        # 3. Pre-load public DB capability overlay so first chat avoids
        #    scanning admin_skills/admin_mcp_servers on the request path.
        def _warm_runtime_catalog() -> None:
            from core.capabilities.catalog.runtime import warmup_runtime_catalog_cache

            warmup_runtime_catalog_cache()
            logger.info("[startup] Runtime catalog DB overlay warmed")

        await _run_sync("Runtime catalog", _warm_runtime_catalog)

        # 5. Pre-load DB prompt parts so first chat skips DB query
        from prompts.prompt_runtime import warmup_prompt_cache

        await _run_sync("Prompt cache", warmup_prompt_cache)

        # 6. Idempotent backfill of navigation entries that were added to
        #    DEFAULT_PAGE_CONFIG after this env first seeded its page_config
        #    row. See _NAV_BACKFILL_ENTRIES in content_blocks.py — append a
        #    new entry there each time we add a sidebar feature.
        def _backfill_nav():
            from core.files.content.branding import backfill_navigation_entries
            from core.db.engine import SessionLocal

            db = SessionLocal()
            try:
                changed = backfill_navigation_entries(db)
                if changed:
                    logger.info(
                        "[startup] page_config navigation backfilled (%d field(s))", changed
                    )
            finally:
                db.close()

        await _run_sync("Page config nav backfill", _backfill_nav)

        elapsed = time.monotonic() - start
        logger.info("[startup] Preload complete in %.2fs", elapsed)

    asyncio.create_task(_run())

async def _startup_automation_scheduler():
    """Start the automation scheduler for timed task execution."""
    if not _env_flag("AUTOMATION_ENABLED", True):
        logger.info("[startup] Automation scheduler disabled")
        return
    try:
        from orchestration.automations.automation_scheduler import AutomationScheduler

        global _automation_scheduler
        _automation_scheduler = AutomationScheduler()
        await _automation_scheduler.start()
        from core.infra import runtime_state

        runtime_state.register("automation_scheduler", _automation_scheduler)
    except Exception as exc:
        logger.warning("[startup] Automation scheduler failed to start: %s", exc)

async def _startup_kb_wiki_worker():
    """Start the knowledge-base Wiki generation worker.

    Wiki 生成是分钟级到小时级的长任务，作业状态落在 kb_wiki_jobs 表上，worker 认领
    后写心跳。启动时会自动接手上次进程留下的僵尸作业，不需要额外的恢复步骤。
    """
    if not _env_flag("KB_WIKI_ENABLED", True):
        logger.info("[startup] KB wiki worker disabled")
        return
    try:
        from features.knowledge.wiki.worker import WikiIngestWorker

        global _kb_wiki_worker
        _kb_wiki_worker = WikiIngestWorker()
        await _kb_wiki_worker.start()
        from core.infra import runtime_state

        runtime_state.register("kb_wiki_worker", _kb_wiki_worker)
    except Exception as exc:
        logger.warning("[startup] KB wiki worker failed to start: %s", exc)

async def _startup_kb_index_worker():
    """Start the knowledge-base document indexing worker.

    索引（解析→分块→向量化）以前挂在 FastAPI BackgroundTasks 上，会抢 Starlette 的
    共享请求线程池——批量上传时把池占满，所有接口在进 handler 之前就排队，前端一直
    转圈、网关报 502；而且任务是进程内的，重启即丢，文档永远停在「索引中」。现在改
    成持久化队列 + 独立线程池的常驻 worker，启动时自动接手上个进程留下的僵死文档。
    """
    if not _env_flag("KB_INDEX_WORKER_ENABLED", True):
        logger.info("[startup] KB index worker disabled")
        return
    try:
        from features.knowledge.index_worker import KBIndexWorker

        global _kb_index_worker
        _kb_index_worker = KBIndexWorker()
        await _kb_index_worker.start()
        from core.infra import runtime_state

        runtime_state.register("kb_index_worker", _kb_index_worker)
    except Exception as exc:
        logger.warning("[startup] KB index worker failed to start: %s", exc)

async def _shutdown_kb_index_worker():
    global _kb_index_worker
    if _kb_index_worker is None:
        return
    try:
        await _kb_index_worker.stop()
    except Exception as exc:
        logger.warning("[shutdown] KB index worker stop failed: %s", exc)
    _kb_index_worker = None

async def _shutdown_kb_wiki_worker():
    global _kb_wiki_worker
    if _kb_wiki_worker is None:
        return
    try:
        await _kb_wiki_worker.stop()
    except Exception as exc:
        logger.warning("[shutdown] KB wiki worker stop failed: %s", exc)
    _kb_wiki_worker = None

async def _startup_channel_manager():
    """Start the inbound channel bot long connections (owner service-account model)."""
    if not _env_flag("CHANNEL_BOTS_ENABLED", True):
        logger.info("[startup] Channel bots disabled")
        return
    try:
        from integrations.channels.manager import get_manager

        await get_manager().start_all()
        from core.infra import runtime_state

        runtime_state.register("channel_manager", get_manager())
    except Exception as exc:
        logger.warning("[startup] Channel manager failed to start: %s", exc)

_automation_scheduler = None

_kb_wiki_worker = None

_kb_index_worker = None

_distillation_scheduler = None

_idle_reaper_task = None

async def _startup_distillation_scheduler():
    """Start the daily skill-distillation cron scheduler."""
    try:
        from orchestration.workers.edition_startup import create_distillation_scheduler

        global _distillation_scheduler
        _distillation_scheduler = create_distillation_scheduler()
        if _distillation_scheduler is None:
            return
        await _distillation_scheduler.start()
        from core.infra import runtime_state

        runtime_state.register("distillation_scheduler", _distillation_scheduler)
    except Exception as exc:
        logger.warning("[startup] Distillation cron scheduler failed to start: %s", exc)

async def _shutdown_pools():
    """Close chat MCP sessions and application services on shutdown."""
    from core.llm.mcp.chat_sessions import close_chat
    await close_chat()
    # Stop automation scheduler — same handler the leader path calls, so a
    # deposed leader and an exiting process cannot stop different things.
    try:
        await _shutdown_automation_scheduler()
    except Exception as e:
        logger.warning("automation_scheduler_shutdown_error", error=str(e))
    # Stop distillation scheduler
    global _distillation_scheduler
    if _distillation_scheduler is not None:
        try:
            await _distillation_scheduler.stop()
        except Exception as e:
            logger.warning("distillation_scheduler_shutdown_error", error=str(e))
    # Stop idle sandbox-session reaper
    try:
        await _shutdown_idle_session_reaper()
    except Exception as e:
        logger.warning("idle_reaper_shutdown_error", error=str(e))
    import asyncio

    from core.harness.observation_writer import flush_observations

    # Hook stage events and usage are written in the background; persist the
    # tail before the database and Redis go away.
    await asyncio.to_thread(flush_observations, 5.0)

    try:
        from core.infra.redis import close_redis

        await close_redis()
    except Exception as e:
        logger.warning("redis_shutdown_error", error=str(e))
