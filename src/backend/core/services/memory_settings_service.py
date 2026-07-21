"""Memory capability checks and effective per-user defaults."""

from __future__ import annotations

import importlib.util
from typing import Any, Dict

from core.config.settings import settings
from core.db import model_repository
from core.infra.exceptions import BadRequestError
from sqlalchemy.orm import Session


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _memory_runtime_available() -> bool:
    """Return whether this process has the configured mem0 runtime."""
    if not settings.memory.enabled or not _module_available("mem0"):
        return False
    if settings.deploy.is_local and not _module_available("milvus_lite"):
        return False
    return True


class MemorySettingsService:
    """Resolve memory availability and guard user-facing memory switches."""

    def __init__(self, db: Session):
        self.db = db

    def embedding_available(self) -> bool:
        provider = model_repository.get_active_role_provider(
            self.db,
            "embedding",
            provider_type="embedding",
        )
        return bool(
            provider and (provider.base_url or "").strip() and (provider.model_name or "").strip()
        )

    def availability(self) -> Dict[str, bool]:
        mem0_available = _memory_runtime_available()
        embedding_available = self.embedding_available()
        return {
            "mem0_available": mem0_available,
            "embedding_available": embedding_available,
            "memory_available": mem0_available and embedding_available,
        }

    def apply_effective_defaults(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Apply default-on semantics while forcing unavailable memory off."""
        result = dict(metadata)
        available = self.availability()["memory_available"]
        result["memory_enabled"] = available and bool(metadata.get("memory_enabled", True))
        result["memory_write_enabled"] = available and bool(
            metadata.get("memory_write_enabled", True)
        )
        return result

    def validate_patch(self, patch: Dict[str, Any]) -> None:
        """Reject attempts to enable memory without its required dependencies."""
        enabling = patch.get("memory_enabled") is True or patch.get("memory_write_enabled") is True
        if not enabling:
            return

        availability = self.availability()
        if not availability["mem0_available"]:
            raise BadRequestError("当前实例未配置可用的记忆服务")
        if not availability["embedding_available"]:
            raise BadRequestError("开启记忆前请先配置并分配 embedding 模型")
