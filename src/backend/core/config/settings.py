"""Environment-backed configuration values."""
from __future__ import annotations
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from .environment import _env, _bool, _int, _float, _REPO_ROOT, DEFAULT_CHAT_MODEL_ALIAS, _load_env_files

from .identity_settings import (
    AuthSettings,
    SSOSettings,
    SessionSettings,
    OASsoSettings,
    DatabaseSettings,
)
from .runtime_settings import (
    LLMSettings,
    MemorySettings,
    StorageSettings,
    KnowledgeBaseSettings,
    RedisSettings,
    ServerSettings,
    RateLimitSettings,
    RoutingSettings,
    CompactionSettings,
    PromptSettings,
)
from .sandbox_settings import (
    SandboxSettings,
)
from .product_settings import (
    EditionSettings,
    GatewaySettings,
    BrandingSettings,
    DeploySettings,
)

@dataclass(frozen=True)
class AppSettings:
    """Top-level settings container — one read from env at startup."""

    deploy: DeploySettings = field(default_factory=DeploySettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    sso: SSOSettings = field(default_factory=SSOSettings)
    oa_sso: OASsoSettings = field(default_factory=OASsoSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    db: DatabaseSettings = field(default_factory=DatabaseSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    kb: KnowledgeBaseSettings = field(default_factory=KnowledgeBaseSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    rate_limit: RateLimitSettings = field(default_factory=RateLimitSettings)
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    compaction: CompactionSettings = field(default_factory=CompactionSettings)
    prompt: PromptSettings = field(default_factory=PromptSettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    edition: EditionSettings = field(default_factory=EditionSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    branding: BrandingSettings = field(default_factory=BrandingSettings)


# Singleton — import this everywhere
settings = AppSettings()
