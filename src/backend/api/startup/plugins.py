"""Application lifecycle operations."""
import os
import sys
from core.config.settings import settings
from core.infra.logging import get_logger
logger = get_logger(__name__)

from core.db.threads import run_db

def _seed_mcp_servers():
    """Seed the built-in global MCP catalog if the DB has none (idempotent).

    On compose these rows come from alembic seed migrations; on the no-Docker
    local profile the DB is built by ``create_all()`` (alembic never runs), so the
    catalog would be empty. Only fires when no global built-in row exists, so it
    bootstraps a fresh install without touching alembic-seeded DBs or resurrecting
    admin-deleted rows. Missing table (CE edge) degrades to a no-op.
    """
    try:
        from core.db.engine import SessionLocal
        from core.capabilities.mcp.management import encrypt_legacy_mcp_headers
        from features.marketplace.mcp_curated import ensure_curated_market_items
        from core.capabilities.mcp.service import prune_removed_builtin_mcp_servers, seed_builtin_mcp_servers_if_empty

        db = SessionLocal()
        try:
            pruned = prune_removed_builtin_mcp_servers(db)
            if pruned:
                logger.info("[startup] unavailable built-in MCP rows pruned: %s", ", ".join(pruned))
            seeded = seed_builtin_mcp_servers_if_empty(db)
            if seeded:
                logger.info("[startup] built-in MCP catalog seeded: %s", ", ".join(seeded))
            encrypted = encrypt_legacy_mcp_headers(db)
            if encrypted:
                logger.info("[startup] encrypted legacy MCP header rows: %d", encrypted)
            curated = ensure_curated_market_items(db)
            if curated:
                logger.info("[startup] curated MCP marketplace seeded: %s", ", ".join(curated))
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[startup] MCP catalog seed failed: %s", exc)


async def _startup_seed_mcp_servers():
    await run_db(_seed_mcp_servers)

async def _startup_mcp_market_monitor():
    """Start periodic remote-tool drift detection after MCP rows are available."""
    try:
        from features.marketplace.mcp_monitor import start_monitor

        start_monitor()
    except Exception as exc:
        logger.warning("[startup] MCP marketplace monitor failed to start: %s", exc)

async def _shutdown_mcp_market_monitor():
    try:
        from features.marketplace.mcp_monitor import stop_monitor

        await stop_monitor()
    except Exception as exc:
        logger.warning("[shutdown] MCP marketplace monitor failed: %s", exc)

async def _startup_upgrade_sites_plugin():
    """Refresh installed builtin site instructions in both cloud and local editions."""
    import asyncio

    from core.db.engine import SessionLocal
    from features.sites.builtin_upgrade import upgrade_builtin_sites

    def upgrade():
        with SessionLocal() as db:
            return upgrade_builtin_sites(db)

    count = await asyncio.to_thread(upgrade)
    if count:
        logger.info("[startup] upgraded %d builtin sites installation(s)", count)

async def _startup_migrate_local_capabilities():
    """Finish capability identity migration before requests freeze allowlists."""
    from core.config.local_mode import local_mode_enabled
    if not local_mode_enabled():
        return
    from core.harness.registry.local_publication import migrate_local_plugins, migrate_local_skills
    await run_db(migrate_local_plugins)
    await run_db(migrate_local_skills)


async def _startup_plugin_device_assets():
    """给这台机器上已有的插件补齐 / 刷新它们的本机资产。

    资产随安装包发布：插件本身没变时既不会重新安装、也不会重新同步，升级带来的修复
    就落不到位。这一步只覆盖"能力已经在这台机器上"的插件，不会凭空铺没人用的东西。
    """
    import asyncio

    from core.config.local_mode import local_mode_enabled

    if not local_mode_enabled():
        return

    from core.db.engine import SessionLocal
    from features.desktop.local.plugins.assets import provision_present_plugins

    def provision():
        with SessionLocal() as db:
            return provision_present_plugins(db)

    ready = await asyncio.to_thread(provision)
    if ready:
        logger.info("[startup] 本机资产已就绪：%s", ", ".join(ready))

async def _startup_backfill_tool_schemas():
    """Capture real tool schemas for servers a plugin manifest only named."""
    from core.capabilities.mcp.tool_schema_backfill import backfill_missing_tool_schemas

    count = await backfill_missing_tool_schemas()
    if count:
        logger.info("[startup] tool schemas captured for %d MCP server(s)", count)

async def _startup_backfill_api_protocol():
    """Record which OpenAI wire protocol each pre-existing model endpoint speaks."""
    from core.models.api_protocol_backfill import backfill_missing_api_protocol

    count = await backfill_missing_api_protocol()
    if count:
        logger.info("[startup] api_protocol recorded for %d model provider(s)", count)

def _seed_default_plugins():
    """Install edition-specific default plugins once per persistent database.

    Local/desktop installs run the equivalent bootstrap in ``cli.py`` before
    the API is imported. Compose needs a database-backed marker so a user who
    later uninstalls one of the defaults does not have it resurrected after a
    container restart. Edition-specific bundles are delegated through the
    startup seam so CE never imports enterprise implementations.
    """
    edition = settings.edition.edition
    if edition not in {"ce", "ee"}:
        return
    if settings.deploy.is_local:
        from features.desktop.capabilities import device_catalog

        # Hybrid capabilities come from the authenticated cloud account. Local
        # defaults also respect the installer's explicit onboarding opt-in.
        if os.getenv("HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS") != "1" or device_catalog.active():
            return

    from core.db.engine import SessionLocal
    from orchestration.workers.edition_startup import bootstrap_edition_plugins
    from core.capabilities.plugins.management import DEFAULT_BOOTSTRAP_PLUGIN_SLUGS, ensure_default_plugins_bootstrapped
    from features.workbench.ui.contributions import refresh_builtin_ui_contributions
    from core.capabilities.plugins.management.defaults import ensure_visualize_plugin_bootstrapped

    db = SessionLocal()
    try:
        if edition == "ce" and not settings.deploy.is_local and ensure_default_plugins_bootstrapped(db):
            logger.info(
                "[startup] default plugins bootstrapped: %s",
                ", ".join(DEFAULT_BOOTSTRAP_PLUGIN_SLUGS),
            )
        ensure_visualize_plugin_bootstrapped(db)
        edition_plugins = bootstrap_edition_plugins(db) if edition == "ee" else ()
        if edition_plugins:
            logger.info(
                "[startup] edition plugins bootstrapped: %s",
                ", ".join(edition_plugins),
            )
        # UI 贡献声明与安装记录同表存储，但它是展示配置、不携带用户状态：
        # 每次启动与 bundle 清单对齐，存量安装（列为 NULL）和改过 ui 声明
        # 未抬版本的 bundle 才能在重启后拿到最新界面贡献。
        refresh_builtin_ui_contributions(db)
    except Exception as exc:
        logger.error("[startup] default plugin bootstrap failed: %s", exc)
        # These plugins preserve advertised edition capabilities. Do not report
        # a healthy deployment with only a partial/default-missing toolset.
        raise
    finally:
        db.close()


async def _startup_seed_default_plugins():
    await run_db(_seed_default_plugins)
