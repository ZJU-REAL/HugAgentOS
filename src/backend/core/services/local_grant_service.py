"""Local-mode folder grants + danger-command policy store (ticket #06).

A desktop-local, single-user store of which host folders the agent is authorized
to touch (beyond the default workspace root) and how each danger-command class is
handled. Persisted as one JSON file under the local data dir (``~/.hugagent`` by
default), so it survives restarts and is shared between the shell (which writes
grants when the user authorizes a folder) and the backend (which reads them in
the execution policy gate).

Edition-agnostic shared ``core`` module; only used by the desktop local backend.
The gate integration and the routes are both gated on ``local_mode_enabled()``,
so the cloud/web deployment never reads or exposes any of this.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

# 分类词表只有一处真源（分类器自己），这里跟着它走：新增一类危险操作不必再同步改一遍
# 校验、设置面板和「完全放开」那三处。
from core.sandbox.local_policy import DANGER_CATEGORIES as _DANGER_CATEGORIES

_LOCK = threading.Lock()
_VALID_MODES = ("read", "readwrite")
_VALID_DISPOSITIONS = ("block", "confirm", "allow")


def _data_dir() -> Path:
    from core.config.runtime_env import local_data_dir

    return local_data_dir()


def _store_path() -> Path:
    return _data_dir() / "local_grants.json"


def _default_state() -> Dict[str, Any]:
    return {"grants": [], "policy": {}}


def _canonical_grant_path(path: str) -> str:
    raw = (path or "").strip()
    return os.path.realpath(os.path.abspath(os.path.expanduser(raw))) if raw else ""


def _load() -> Dict[str, Any]:
    path = _store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        data.setdefault("grants", [])
        data.setdefault("policy", {})
        return data
    except (OSError, ValueError):
        return _default_state()


def _save(data: Dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_grants() -> List[Dict[str, Any]]:
    return list(_load().get("grants", []))


def add_grant(path: str, mode: str = "readwrite") -> Dict[str, Any]:
    real = _canonical_grant_path(path)
    if not real:
        raise ValueError("授权路径不能为空")
    if mode not in _VALID_MODES:
        raise ValueError(f"mode 必须是 {_VALID_MODES}")
    with _LOCK:
        data = _load()
        grants = [
            g
            for g in data.get("grants", [])
            if _canonical_grant_path(str(g.get("path") or "")) != real
        ]
        entry = {"path": real, "mode": mode}
        grants.append(entry)
        data["grants"] = grants
        _save(data)
    return entry


def remove_grant(path: str) -> None:
    real = _canonical_grant_path(path)
    with _LOCK:
        data = _load()
        data["grants"] = [
            g
            for g in data.get("grants", [])
            if _canonical_grant_path(str(g.get("path") or "")) != real
        ]
        _save(data)


def get_policy() -> Dict[str, str]:
    return dict(_load().get("policy", {}))


def set_policy(policy: Dict[str, str]) -> Dict[str, str]:
    clean: Dict[str, str] = {}
    for key, value in (policy or {}).items():
        if key == "out_of_scope" or key in _DANGER_CATEGORIES:
            if value not in _VALID_DISPOSITIONS:
                raise ValueError(f"{key} 的取值必须是 {_VALID_DISPOSITIONS}")
            clean[key] = value
    with _LOCK:
        data = _load()
        data["policy"] = clean
        _save(data)
    return clean


def grants_for_gate() -> List[Any]:
    """Convert stored grants into ``local_policy.Grant`` objects for the gate."""
    from core.sandbox.local_policy import Grant

    return [
        Grant(path=g["path"], mode=g.get("mode", "readwrite"))
        for g in list_grants()
        if g.get("path")
    ]


def policy_for_gate(approval_mode: str) -> Any:
    """Build the effective ``local_policy.Policy`` for one run's permission preset.

    桌面端不再另存一份「本机操作权限档」——粗档就是用户在输入框那颗胶囊里选的
    那一档（``core.llm.tool_permissions`` 的 ask / auto / full），本文件只负责把
    它翻译成本机策略：

      - ``full``      ：全部放行（仅记审计），完全不管。
      - ``ask``/``auto``：走用户配置的分类处置 / 内置默认（中间地带）；两档在
        本机策略上一致，区别是"要不要停下来问"，那由权限网关按危险类别决定。
    """
    from core.sandbox.local_policy import Policy

    if approval_mode == "full":
        return Policy(
            out_of_scope="allow",
            workspace_write="allow",
            danger={category: "allow" for category in _DANGER_CATEGORIES},
        )
    stored = get_policy()
    out_of_scope = stored.get("out_of_scope", "confirm")
    danger = {k: v for k, v in stored.items() if k in _DANGER_CATEGORIES}
    return Policy(out_of_scope=out_of_scope, workspace_write="allow", danger=danger)


__all__ = [
    "list_grants",
    "add_grant",
    "remove_grant",
    "get_policy",
    "set_policy",
    "grants_for_gate",
    "policy_for_gate",
]
