"""Account configuration passed to backend tools and sandbox commands."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)




def _firecrawl_extra_envs() -> dict[str, str]:
    """Inject deployment-level credentials for the firecrawl CLI (firecrawl-cli is preinstalled in the sandbox and authenticates via env vars).

    **The config source is the Config admin "System Config → firecrawl" DB config**
    (``firecrawl.api_key`` / ``firecrawl.api_url``, the sole authority; no fallback to
    .env/compose). Both modes are supported, decided by which item is configured:
    - Cloud: configure ``firecrawl.api_key`` → inject ``FIRECRAWL_API_KEY``.
    - Self-hosted: configure ``firecrawl.api_url`` → inject ``FIRECRAWL_API_URL`` (the CLI
      skips cloud auth when it sees api_url); if a self-hosted instance also needs a key,
      both items can coexist.
    If neither is configured return an empty dict (equivalent to the original behavior;
    firecrawl commands fail unauthenticated and guide the user to configure it in the
    admin console). All users share one config; takes effect within <=30s of saving
    (SystemConfigService 30s cache), no restart needed.
    """
    out: dict[str, str] = {}
    try:
        from core.config.store.system_config import SystemConfigService

        svc = SystemConfigService.get_instance()
        api_key = (svc.get("firecrawl.api_key") or "").strip()
        api_url = (svc.get("firecrawl.api_url") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[firecrawl] read firecrawl config failed: %s", exc)
        return out
    if api_key:
        out["FIRECRAWL_API_KEY"] = api_key
    if api_url:
        out["FIRECRAWL_API_URL"] = api_url
    return out
