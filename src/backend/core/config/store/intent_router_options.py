"""智能路由（``mode_slug=auto``）的系统配置读取。

控制源 = Config 管理台「系统配置 → 智能路由」的 ``intent_router.*``，保存后 ≤30s 生效。
读配置出任何异常都按「未开启」处理：路由是体验增强，不能因为配置层抖动让对话起不来。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config.store import system_config

STRATEGY_SMART_KEEP = "smart_keep"
STRATEGY_PER_TURN = "per_turn"
STRATEGY_FIRST_TURN = "first_turn"
STRATEGIES = (STRATEGY_SMART_KEEP, STRATEGY_PER_TURN, STRATEGY_FIRST_TURN)

DEFAULT_FALLBACK_SLUG = "standard"
DEFAULT_TIMEOUT_MS = 5000
_MIN_TIMEOUT_MS = 500
_MAX_TIMEOUT_MS = 60_000


@dataclass(frozen=True)
class IntentRouterOptions:
    enabled: bool = False
    strategy: str = STRATEGY_SMART_KEEP
    #: 空元组 = 全部启用的官方模式。
    candidate_slugs: tuple[str, ...] = ()
    fallback_slug: str = DEFAULT_FALLBACK_SLUG
    timeout_ms: int = DEFAULT_TIMEOUT_MS


DISABLED = IntentRouterOptions()


def truthy(raw: object) -> bool:
    """A switch value from system config (shared with the other side-call options)."""
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _slug_list(raw: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(p.strip() for p in str(raw or "").split(",") if p.strip()))


def timeout_ms(raw: object) -> int:
    """A side-call timeout in ms, clamped to 0.5–60 s; unparsable reads as the default."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    return min(_MAX_TIMEOUT_MS, max(_MIN_TIMEOUT_MS, value))


def intent_router_options() -> IntentRouterOptions:
    """当前生效的智能路由配置；配置层异常时返回「未开启」。"""
    try:
        get = system_config.SystemConfigService.get_instance().get
        enabled = truthy(get("intent_router.enable", "false"))
        strategy = str(get("intent_router.strategy", STRATEGY_SMART_KEEP) or "").strip().lower()
        candidates = _slug_list(get("intent_router.candidate_slugs", ""))
        fallback = str(get("intent_router.fallback_slug", DEFAULT_FALLBACK_SLUG) or "").strip()
        timeout = timeout_ms(get("intent_router.timeout_ms", str(DEFAULT_TIMEOUT_MS)))
    except Exception:  # noqa: BLE001 — 配置层异常按未开启处理
        return DISABLED
    return IntentRouterOptions(
        enabled=enabled,
        strategy=strategy if strategy in STRATEGIES else STRATEGY_SMART_KEEP,
        candidate_slugs=candidates,
        fallback_slug=fallback or DEFAULT_FALLBACK_SLUG,
        timeout_ms=timeout,
    )


def intent_router_enabled() -> bool:
    return intent_router_options().enabled
