"""Model context window policy.

Owns the window itself and the two fractions every layer budgets against: how
much of it a request may occupy, and how full it may get before compaction
runs. Keeping both here is the point — output headroom is carried once, as
``EFFECTIVE_CONTEXT_WINDOW_RATIO``, instead of each layer subtracting its own
guess at the model's ``max_tokens``.
"""

from __future__ import annotations

# Share of the window a request's input may occupy. The remainder is the single
# headroom allowance covering model output and protocol overhead.
EFFECTIVE_CONTEXT_WINDOW_RATIO = 0.95

# Ceiling for the auto-compaction trigger. Whatever ratio or explicit token
# limit is configured, compaction never waits past this share of the window.
AUTO_COMPACT_MAX_RATIO = 0.90

# Window assumed for a model that declares none. Deliberately small: assembling
# against a window wider than the model's would overflow the request outright.
FALLBACK_CONTEXT_WINDOW = 32_768


def usable_context_window(context_window: int) -> int:
    """Input tokens a request may occupy in ``context_window``."""
    return max(0, int(context_window * EFFECTIVE_CONTEXT_WINDOW_RATIO))


def resolve_model_context_window(model_name: str) -> int:
    """Read the context window size from the Config admin platform's model configuration.

    Data source: ModelProvider.extra_config.context_length (DB-driven, single source of truth).
    **No default fallback**: unconfigured / empty model name / query failure all raise
    ``ValueError``, forcing every model's real context_length to be filled in under
    Config admin → model configuration — the silent 128k fallback once caused a real
    256k model to repeatedly trigger compression at half its window.
    Callers that can tolerate "no window, just skip" (e.g. the compression-trigger check)
    should try/except themselves.
    """
    if not model_name:
        raise ValueError(
            "无法解析模型上下文窗口：模型名为空。请检查模型配置（Config 后管 → 模型配置）。"
        )

    try:
        from core.services.model_config import ModelConfigService

        ctx_len = ModelConfigService.get_instance().get_context_length_by_model_name(model_name)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"查询模型 '{model_name}' 的上下文窗口配置失败：{exc}") from exc
    if ctx_len and ctx_len > 0:
        return ctx_len

    raise ValueError(
        f"模型 '{model_name}' 未配置 context_length（上下文窗口）。"
        "请到 Config 后管平台 → 模型配置中为该模型补齐真实上下文长度。"
    )
