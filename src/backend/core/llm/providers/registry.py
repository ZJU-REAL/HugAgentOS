"""Provider registry — single source of truth for vendors/protocols.

Each provider declares: engine + supported uses (provider_type) + base_url
template + vendor-specific extra fields. Backend model dispatch, connectivity
tests, field validation, and the frontend dynamic form all read from here,
avoiding duplicate field definitions on frontend and backend.

Engines:
- ``openai``   : OpenAI-compatible protocol (including OpenAI-compatible vendor presets + Azure OpenAI), via OpenAICompatChatModel.
- ``native``   : AgentScope 2.0 native model classes (Anthropic / Gemini / DashScope / Ollama).
- ``litellm``  : vendors adapted through litellm (AWS Bedrock and the long tail), via LiteLLMChatModel.

To add an OpenAI-compatible vendor: just add an engine="openai" preset to
PROVIDER_SPECS (zero migration, zero new protocol). Vendor-specific
credentials (api_version / deployment / aws_region ...) always land in the
extra_config JSONB; adding vendors never touches the table schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProviderField:
    """One extra field of a provider (beyond the common base_url/api_key/model_name)."""
    key: str
    label: str
    required: bool = False
    secret: bool = False
    placeholder: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    engine: str                       # "openai" | "native" | "litellm"
    native_class: str = ""            # AgentScope class name when engine="native"
    litellm_prefix: str = ""          # model prefix when engine="litellm", e.g. "bedrock/"
    supports_types: tuple[str, ...] = ("chat",)
    base_url_template: str = ""        # frontend placeholder / default base_url hint
    # Whether base_url is definitively fixed: when True, selecting this vendor in
    # the frontend auto-fills base_url_template into the input box.
    # Generic vendors (bring their own endpoint), templates with <...>
    # placeholders, or vendors without a fixed URL stay False.
    autofill_base_url: bool = False
    api_key_required: bool = True
    fields: tuple[ProviderField, ...] = ()  # vendor-specific extra fields (stored in extra_config)

    @property
    def extra_field_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields)


# ── Field groups (reused) ─────────────────────────────────────────────────────
_AZURE_FIELDS = (
    ProviderField("api_version", "API 版本", required=True, placeholder="2024-06-01"),
    ProviderField("deployment", "部署名 Deployment", required=True, placeholder="gpt-4o"),
)
_BEDROCK_FIELDS = (
    ProviderField("aws_region", "AWS 区域", required=True, placeholder="us-east-1"),
    ProviderField("aws_access_key_id", "Access Key ID", required=True, secret=True),
    ProviderField("aws_secret_access_key", "Secret Access Key", required=True, secret=True),
)


# ── Provider list ─────────────────────────────────────────────────────────────
PROVIDER_SPECS: dict[str, ProviderSpec] = {
    # —— OpenAI-compatible (generic + presets; engine=openai, no new protocol) ——
    "openai_compatible": ProviderSpec(
        id="openai_compatible", label="OpenAI 兼容", engine="openai",
        supports_types=("chat", "embedding", "reranker"),
        base_url_template="https://api.openai.com/v1",
    ),
    "deepseek": ProviderSpec(
        id="deepseek", label="DeepSeek", engine="openai",
        base_url_template="https://api.deepseek.com/v1", autofill_base_url=True,
    ),
    "zhipu": ProviderSpec(
        id="zhipu", label="智谱 GLM", engine="openai",
        base_url_template="https://open.bigmodel.cn/api/paas/v4", autofill_base_url=True,
    ),
    "minimax": ProviderSpec(
        id="minimax", label="MiniMax", engine="openai",
        base_url_template="https://api.minimaxi.com/v1", autofill_base_url=True,
    ),
    "siliconflow": ProviderSpec(
        id="siliconflow", label="硅基流动", engine="openai",
        supports_types=("chat", "embedding", "reranker"),
        base_url_template="https://api.siliconflow.cn/v1", autofill_base_url=True,
    ),
    "moonshot": ProviderSpec(
        id="moonshot", label="Kimi / Moonshot", engine="openai",
        base_url_template="https://api.moonshot.cn/v1", autofill_base_url=True,
    ),
    "qwen_compat": ProviderSpec(
        id="qwen_compat", label="通义千问（兼容模式）", engine="openai",
        supports_types=("chat", "embedding"),
        base_url_template="https://dashscope.aliyuncs.com/compatible-mode/v1", autofill_base_url=True,
    ),
    "azure_openai": ProviderSpec(
        id="azure_openai", label="Azure OpenAI", engine="openai",
        base_url_template="https://<resource>.openai.azure.com",
        fields=_AZURE_FIELDS,
    ),
    # —— AgentScope native providers (non-OpenAI protocol) ——
    "anthropic": ProviderSpec(
        id="anthropic", label="Anthropic 原生", engine="native",
        native_class="AnthropicChatModel",
        base_url_template="https://api.anthropic.com", autofill_base_url=True,
    ),
    "gemini": ProviderSpec(
        id="gemini", label="Google Gemini", engine="native",
        native_class="GeminiChatModel",
    ),
    "dashscope": ProviderSpec(
        id="dashscope", label="阿里 DashScope 原生", engine="native",
        native_class="DashScopeChatModel",
    ),
    "ollama": ProviderSpec(
        id="ollama", label="本地 Ollama", engine="native",
        native_class="OllamaChatModel",
        base_url_template="http://localhost:11434", autofill_base_url=True,
        api_key_required=False,
    ),
    # —— litellm-adapted providers ——
    "bedrock": ProviderSpec(
        id="bedrock", label="AWS Bedrock", engine="litellm",
        litellm_prefix="bedrock/",
        base_url_template="",
        api_key_required=False,
        fields=_BEDROCK_FIELDS,
    ),
}

_DEFAULT_ID = "openai_compatible"


def get_spec(provider_id: Optional[str]) -> ProviderSpec:
    """Return the provider spec; unknown/empty falls back to openai_compatible (backward compatible with existing data)."""
    if not provider_id:
        return PROVIDER_SPECS[_DEFAULT_ID]
    return PROVIDER_SPECS.get(provider_id, PROVIDER_SPECS[_DEFAULT_ID])


def is_known(provider_id: Optional[str]) -> bool:
    return bool(provider_id) and provider_id in PROVIDER_SPECS


def list_specs(provider_type: Optional[str] = None) -> list[ProviderSpec]:
    specs = list(PROVIDER_SPECS.values())
    if provider_type:
        specs = [s for s in specs if provider_type in s.supports_types]
    return specs


def split_provider_extra(spec: ProviderSpec, extra_config: dict) -> dict:
    """Pick out this provider's vendor credential fields (spec.fields) from extra_config."""
    keys = set(spec.extra_field_keys)
    return {k: v for k, v in (extra_config or {}).items() if k in keys}


def validate_payload(provider_id: str, provider_type: str, extra_config: dict) -> Optional[str]:
    """Validate a single provider config. Returns an error message string, or None if valid."""
    if not is_known(provider_id):
        return f"未知的 provider：{provider_id}"
    spec = get_spec(provider_id)
    if provider_type not in spec.supports_types:
        return f"{spec.label} 不支持用途 '{provider_type}'（支持：{', '.join(spec.supports_types)}）"
    extra = extra_config or {}
    for f in spec.fields:
        if f.required and not str(extra.get(f.key, "")).strip():
            return f"{spec.label} 需要填写「{f.label}」"
    return None


def to_frontend_schema() -> list[dict]:
    """Serves GET /v1/models/provider-schemas, driving the frontend dynamic form."""
    return [
        {
            "id": s.id,
            "label": s.label,
            "engine": s.engine,
            "supports_types": list(s.supports_types),
            "base_url_template": s.base_url_template,
            "autofill_base_url": s.autofill_base_url,
            "api_key_required": s.api_key_required,
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "required": f.required,
                    "secret": f.secret,
                    "placeholder": f.placeholder,
                }
                for f in s.fields
            ],
        }
        for s in PROVIDER_SPECS.values()
    ]
