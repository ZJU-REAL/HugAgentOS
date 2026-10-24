"""Environment-backed configuration values."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from .environment import _env, _bool, _int

@dataclass(frozen=True)
class EditionSettings:
    """Edition facade (CE/EE). Main repo defaults to ee; the CE derived tree sets JX_EDITION=ce via .env."""

    edition: str = field(default_factory=lambda: _env("JX_EDITION", "ee").strip().lower() or "ee")

    @property
    def is_ee(self) -> bool:
        return self.edition == "ee"

@dataclass(frozen=True)
class GatewaySettings:
    """External model gateway (LiteLLM Proxy data plane).

    The data plane is a standalone LiteLLM container (exposing an
    OpenAI-compatible endpoint); this process is the **control plane**, using
    the LiteLLM admin API (authenticated with ``master_key``) to issue/revoke
    virtual keys and read spend. ``master_key`` is held by the backend only and
    is never sent to the frontend. Without ``master_key`` configured, the
    control-plane endpoints return errors.
    """

    enabled: bool = field(default_factory=lambda: _bool(_env("MODEL_GATEWAY_ENABLED", "false")))
    admin_url: str = field(
        default_factory=lambda: _env("LITELLM_ADMIN_URL", "http://litellm:4000").strip().rstrip("/")
    )
    master_key: str = field(default_factory=lambda: _env("LITELLM_MASTER_KEY", "").strip())
    timeout: int = field(default_factory=lambda: _int(_env("MODEL_GATEWAY_TIMEOUT", "15"), 15))

@dataclass(frozen=True)
class BrandingSettings:
    """Single source of brand defaults — the in-code fallback stays neutral; deployment branding comes from env / the content_blocks DB seed."""

    product_name: str = field(
        default_factory=lambda: _env("BRAND_PRODUCT_NAME", "智能体平台").strip()
    )
    org_name: str = field(default_factory=lambda: _env("BRAND_ORG_NAME", "").strip())
    powered_by_visible: bool = field(
        default_factory=lambda: _bool(_env("BRAND_POWERED_BY", "true"))
    )

@dataclass(frozen=True)
class DeploySettings:
    """Deployment-profile switch.

    ``DEPLOY_PROFILE=local`` = Docker-free single-machine mode (hermes-style
    quick install): a single uvicorn process hosting frontend statics + SQLite
    + in-process event log/ephemeral state + subprocess MCP/sandbox. Default
    empty string = regular compose deployment; no local behavior activates,
    zero impact on existing deployments. Decoupled from capability-specific envs
    (an empty ``REDIS_URL`` / ``DATABASE_URL=sqlite://`` /
    ``SANDBOX_PROVIDER=script_runner``): this switch only toggles **profile**
    behaviors like "in-process hosted sub-services + frontend static mounting";
    each capability is still explicitly driven by its own env (written by the CLI).
    """

    profile: str = field(default_factory=lambda: _env("DEPLOY_PROFILE", "").strip().lower())

    @property
    def is_local(self) -> bool:
        return self.profile == "local"
