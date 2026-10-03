"""Account-owned connector resolution and shared snapshot locking."""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Tuple

from core.db.engine import SessionLocal

# ── 用户有效能力解析（manifest 与网关共用，30s per-user 缓存） ──────────


# 网关每次工具调用都要做归属校验；底层 get_owned_servers 不带缓存（防跨用户
# 泄漏的设计），这里按用户加同节奏的 30s TTL，命中后校验退化为纯内存查找。
_EFFECTIVE_TTL_S = 30.0
_effective_cache: Dict[str, Tuple[float, List[str], List[str], Dict[str, dict]]] = {}
_effective_lock = threading.Lock()


def _user_capability_configs(
    user_id: str, *, use_cache: bool = True
) -> Tuple[List[str], List[str], Dict[str, dict]]:
    """(账号拥有的 server_id, 云端此刻启用的 server_id, {server_id: 已物化连接配置})。

    两个集合的分工，就是「装了什么」与「开着什么」的分工：

    - **拥有集**是管理员放行的全局连接器加上这个用户自己的私有连接器（含他在云端
      关掉的）。清单下发和网关授权都按它来——插件带来的连接器在云端常是关着的，
      按启用集下发会让插件在桌面端只剩个空壳，打开开关也调不通。
    - **启用集**是云端此刻的有效启停，只用来给本机首次落地一个初值。之后开关归
      本机，云端再改也不回头覆盖。

    管理员停用的连接器不在拥有集里，本机也就打不开——这条边界没有放宽。
    配置含云端侧凭据，仅进程内使用。
    """
    uid = str(user_id)
    now = time.monotonic()
    if use_cache:
        with _effective_lock:
            hit = _effective_cache.get(uid)
            if hit and (now - hit[0]) < _EFFECTIVE_TTL_S:
                return list(hit[1]), list(hit[2]), dict(hit[3])

    from core.config.catalog_resolver import resolve_all_runtime_enabled
    from core.llm.agent_factory import _effective_mcp_server_keys
    from core.services.mcp_service import McpServerConfigService

    svc = McpServerConfigService.get_instance()
    owned = svc.get_owned_servers(uid, enabled_only=False, strict=not use_cache)
    with SessionLocal() as db:
        _skills, _agents, mcps = resolve_all_runtime_enabled(db, uid)
    all_cfgs = dict(svc.get_all_servers(enabled_only=True, use_cache=use_cache))
    all_cfgs.update(owned)
    available = list(all_cfgs.keys())
    enabled = _effective_mcp_server_keys(
        None, None, enabled_mcp_ids=list(mcps or []), owned_servers=owned
    )

    with _effective_lock:
        _effective_cache[uid] = (now, list(available), list(enabled), dict(all_cfgs))
    return available, enabled, all_cfgs
