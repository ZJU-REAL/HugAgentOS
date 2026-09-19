"""DB-backed env lookup for MCP servers running in the streamable-http container.

The MCP container's ``os.environ`` is frozen at ``docker-compose up`` time,
so admin-panel changes never reach it. ``get_runtime_value`` re-routes the
existing ``os.getenv`` idiom through ``SystemConfigService`` /
``ModelConfigService`` (both DB-cached for 30s) before falling back to env.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def local_data_dir() -> Path:
    """本机部署的数据根（``HUGAGENT_HOME``，由桌面壳注入）。

    库、能力文件、备份、快照都挂在这个根下。它是一句 ``os.getenv`` 的事，但散抄
    到各处就会在改默认值时漏掉一处，所以只在这里算一次。
    """
    return Path(os.getenv("HUGAGENT_HOME", str(Path.home() / ".hugagent"))).expanduser()

# Env vars produced by ``ModelConfigService.get_mcp_env_overlay()``. Looking up
# a non-model env var should not pay the cost of building that dict.
_MODEL_CONFIG_ENV_VARS: frozenset[str] = frozenset({
    "MODEL_URL", "API_KEY", "BASE_MODEL_NAME",
    "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_KEY",
    "MEM0_EMBED_URL", "MEM0_EMBED_MODEL", "MEM0_EMBED_API_KEY", "MEM0_EMBED_DIMS",
    "RERANKER_URL", "RERANKER_MODEL", "RERANKER_API_KEY",
})


def get_runtime_value(env_var: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve an env-var name against admin DB configs first, then env."""
    try:
        from core.services.system_config import SystemConfigService, get_config_key_for_env

        config_key = get_config_key_for_env(env_var)
        if config_key:
            val = SystemConfigService.get_instance().get(config_key)
            if val:
                return val
    except Exception as exc:
        logger.debug("[runtime_env] system_config lookup failed for %s: %s", env_var, exc)

    if env_var in _MODEL_CONFIG_ENV_VARS:
        try:
            from core.services.model_config import ModelConfigService

            val = ModelConfigService.get_instance().get_mcp_env_overlay().get(env_var)
            if val:
                return val
        except Exception as exc:
            logger.debug("[runtime_env] model_config lookup failed for %s: %s", env_var, exc)

    return os.getenv(env_var, default)
