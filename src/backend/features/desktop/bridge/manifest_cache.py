"""上一次被接受的云端清单落盘，本机后端重启（打开应用 / 更新）后直接复用。

清单在内存里只活一个进程：没有这份缓存，每次重启都要等云端把工具、技能、
智能体、插件四份清单重新下发一遍，能力同步页就得再挡一次。缓存按账号指纹
（云端地址 + 账号 + 登录会话 + 授权轮次 + 设备）分目录，换账号、重新登录或
授权变更都会落到新目录、不会命中；退出登录整体删除。

清单本身不含凭据（工具只保留公开 schema，包内容另按哈希核对），读回时再过一遍
与网络响应相同的校验，被改过的文件直接作废。恢复只是先把上次的视图挂上，
之后照常带 If-None-Match 向云端核对，云端说变了就整体替换。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

KIND_TOOLS = "tools"


def _account_dir(state: Dict[str, Any]) -> Optional[Path]:
    from features.desktop.bridge.identity import state_fingerprint
    from features.desktop.capabilities.paths import capabilities_enabled, manifests_root

    fingerprint = state_fingerprint(state)
    if not fingerprint or not capabilities_enabled():
        return None
    return manifests_root() / hashlib.sha256(fingerprint.encode("ascii")).hexdigest()[:32]


def _validator(kind: str) -> Callable[[Any], Dict[str, Any]]:
    from core.capabilities import manifest_protocol as protocol
    from core.capabilities.identity_rules import KIND_SKILL

    if kind == KIND_TOOLS:
        return protocol.validate_manifest
    if kind == KIND_SKILL:
        return protocol.validate_skill_manifest
    return lambda raw: protocol.validate_entity_manifest(raw, kind)


def save(state: Dict[str, Any], kind: str, manifest: Dict[str, Any]) -> None:
    """Best effort: a failed write only costs one more cloud round trip next start."""
    try:
        directory = _account_dir(state)
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{kind}.json"
        staging = target.with_suffix(".tmp")
        staging.write_text(json.dumps(dict(manifest), ensure_ascii=False), encoding="utf-8")
        os.replace(staging, target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cloud-bridge] manifest cache write skipped kind=%s: %s", kind, type(exc).__name__)


def load(state: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    try:
        directory = _account_dir(state)
        if directory is None:
            return None
        path = directory / f"{kind}.json"
        if not path.is_file():
            return None
        return _validator(kind)(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cloud-bridge] manifest cache ignored kind=%s: %s", kind, type(exc).__name__)
        return None


def clear_all() -> None:
    """Logout forgets every account's snapshot; package files stay with their profiles."""
    try:
        from features.desktop.capabilities.paths import capabilities_enabled, manifests_root

        if not capabilities_enabled():
            return
        root = manifests_root()
        for child in root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cloud-bridge] manifest cache cleanup skipped: %s", type(exc).__name__)
