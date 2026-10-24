"""Thread-safe service that resolves service configs from the database with env fallback."""
from __future__ import annotations
import os
import threading
import time
from typing import Optional
from core.harness.registry.system_configuration import system_configuration_store
import logging
from core.config.store import system_config_constants

logger = logging.getLogger(__name__)

class SystemConfigService:
    """Thread-safe singleton that resolves service configs from DB with env fallback."""

    _instance: Optional["SystemConfigService"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._cache: dict[str, Optional[str]] = {}
        self._cache_meta: dict[str, dict] = {}  # full row metadata
        self._cache_ts: float = 0.0
        self._cache_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "SystemConfigService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── get / get_group ─────────────────────────────────────────────

    def get(self, key: str, default: str | None = None) -> str | None:
        """Get a config value. DB first, then env fallback, then default."""
        self._maybe_refresh()
        if key in self._cache and self._cache[key] is not None:
            return self._cache[key]
        # env fallback
        env_key = system_config_constants.CONFIG_KEY_TO_ENV.get(key)
        if env_key:
            env_val = os.getenv(env_key)
            if env_val is not None:
                return env_val.strip()
        return default

    def get_group(self, group_key: str) -> dict[str, str]:
        """Return all config key→value pairs for a group."""
        self._maybe_refresh()
        result: dict[str, str] = {}
        for key, meta in self._cache_meta.items():
            if meta.get("group_key") == group_key:
                val = self.get(key)
                if val is not None:
                    result[key] = val
        return result

    def get_all_configs(self) -> list[dict]:
        """Return all config rows as dicts (for API responses)."""
        self._maybe_refresh()
        return list(self._cache_meta.values())

    def get_group_configs(self, group_key: str) -> list[dict]:
        """Return config rows for a specific group."""
        self._maybe_refresh()
        return [m for m in self._cache_meta.values() if m.get("group_key") == group_key]

    # ── set ─────────────────────────────────────────────────────────

    def set(self, key: str, value: str | None, updated_by: str = "admin") -> None:
        system_configuration_store().set(key, value, updated_by)
        self.invalidate_cache()

    def bulk_set(self, items: list[dict], updated_by: str = "admin") -> None:
        system_configuration_store().bulk_set(items, updated_by)
        self.invalidate_cache()

    # ── cache management ──────────────────────────────────────────

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()
            self._cache_meta.clear()
            self._cache_ts = 0.0

    def _maybe_refresh(self) -> None:
        now = time.monotonic()
        if now - self._cache_ts < system_config_constants.CACHE_TTL_SECONDS and self._cache_meta:
            return
        with self._cache_lock:
            if now - self._cache_ts < system_config_constants.CACHE_TTL_SECONDS and self._cache_meta:
                return
            self._load_from_db()
            self._cache_ts = time.monotonic()


    def _load_from_db(self) -> None:
        try:
            values, metadata = system_configuration_store().load()
        except Exception as exc:
            logger.warning("[SystemConfigService] DB load failed, keeping stale cache: %s", type(exc).__name__)
            return
        self._cache = values
        self._cache_meta = metadata

    # ── env overlay for MCP sub-processes ─────────────────────────

    def get_service_env_overlay(self) -> dict[str, str]:
        """Return env-var style dict for injecting into MCP sub-processes.

        Maps DB configs to the env var names that MCP servers already read via os.getenv().
        Only includes keys that have a non-None value (from DB or env fallback).
        """
        overlay: dict[str, str] = {}
        for config_key, env_key in system_config_constants.CONFIG_KEY_TO_ENV.items():
            val = self.get(config_key)
            if val is not None:
                overlay[env_key] = val
        return overlay
