"""评价判定（evaluation_gate）的系统配置读取。

控制源 = Config 管理台「系统配置 → 评价判定」的 ``evaluation_gate.*``，保存后 ≤30s 生效。
读配置出任何异常都按「未开启」处理：关闭时所有可评价的回答照旧显示评价卡，这是最安全的状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config.store import system_config

# The switch and timeout parse exactly like the intent router's (same limits, same default).
from core.config.store.intent_router_options import DEFAULT_TIMEOUT_MS, timeout_ms, truthy


@dataclass(frozen=True)
class EvaluationGateOptions:
    enabled: bool = False
    timeout_ms: int = DEFAULT_TIMEOUT_MS


DISABLED = EvaluationGateOptions()


def evaluation_gate_options() -> EvaluationGateOptions:
    """当前生效的评价判定配置；配置层异常时返回「未开启」。"""
    try:
        get = system_config.SystemConfigService.get_instance().get
        enabled = truthy(get("evaluation_gate.enable", "false"))
        timeout = timeout_ms(get("evaluation_gate.timeout_ms", str(DEFAULT_TIMEOUT_MS)))
    except Exception:  # noqa: BLE001 — 配置层异常按未开启处理
        return DISABLED
    return EvaluationGateOptions(enabled=enabled, timeout_ms=timeout)
