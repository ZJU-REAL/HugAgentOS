"""Channel adapter registry (single source of truth, mirroring CE_ROUTERS / platform_registry).

Adding a channel = write an adapter + register it here. Upper layers only fetch via
``get_adapter(channel_type)`` and never import concrete adapters directly.
"""

from __future__ import annotations

import threading
from typing import Dict, List

from core.channels.protocol import ChannelAdapter

_REGISTRY: Dict[str, ChannelAdapter] = {}


def register_adapter(channel_type: str, adapter: ChannelAdapter) -> None:
    _REGISTRY[channel_type] = adapter


def get_adapter(channel_type: str) -> ChannelAdapter:
    if channel_type not in _REGISTRY:
        _ensure_builtin_loaded()
    adapter = _REGISTRY.get(channel_type)
    if adapter is None:
        raise KeyError(f"未知渠道类型: {channel_type}")
    return adapter


def list_adapters() -> List[str]:
    _ensure_builtin_loaded()
    return sorted(_REGISTRY.keys())


_loaded = False
# start_all concurrently spins up multiple long-connection workers, each calling
# get_adapter from its own thread → lazy loading must be thread-safe: otherwise a
# thread that just set _loaded (before the SDK finished importing) would let a later
# thread return early because _loaded==True, getting an empty _REGISTRY → KeyError
# "unknown channel type". Since that call sits outside the while loop in _run_worker,
# raising kills the worker thread permanently (no retry).
_load_lock = threading.Lock()


def _ensure_builtin_loaded() -> None:
    """Lazy-load built-in adapters (avoids hard import-time dependency on each channel SDK). Thread-safe: double-check + lock."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:  # re-check while holding the lock: another thread may have finished loading
            return
        import logging

        log = logging.getLogger(__name__)
        # Each adapter has its own try: a missing SDK/dependency for one channel only disables that channel, without dragging down the others or the process.
        for channel_type, module, cls_name in (
            ("lark", "core.channels.adapters.lark", "LarkAdapter"),
            ("dingtalk", "core.channels.adapters.dingtalk", "DingTalkAdapter"),
            ("wecom", "core.channels.adapters.wecom", "WeComAdapter"),
            ("weixin", "core.channels.adapters.weixin", "WeixinAdapter"),
        ):
            try:
                mod = __import__(module, fromlist=[cls_name])
                register_adapter(channel_type, getattr(mod, cls_name)())
            except Exception:  # noqa: BLE001
                log.warning("[channels] %s adapter 加载失败", channel_type, exc_info=True)
        # Set the flag only after all import attempts complete — must be after the loop, otherwise the race described above recurs.
        _loaded = True
