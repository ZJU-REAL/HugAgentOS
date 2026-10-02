"""Credential snapshots and guarded content/bundle/stream export boundaries."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from core.db.engine import SessionLocal
from core.db.models import ModelProvider
from core.services.desktop_capability_configs import _effective_lock, _user_capability_configs
from core.services.desktop_capability_credentials import (
    CapabilityContentRejected,
    _secrets_from_config,
)


def _without_public_identifiers(secrets: set[str], public_identifiers: set[str]) -> set[str]:
    """与模型的公开标识（provider_id / display_name / model_name，模型选择器里所有登录
    用户都看得到）完全相同的值不是机密：把它当机密只会让清单和模型输出因为自己的
    模型名被拒。"""
    return {value for value in secrets if value.strip() not in public_identifiers}


# Keyed on ModelConfigService.version: every model-row write path bumps it.
_model_secret_cache: Optional[Tuple[int, set[str], set[str]]] = None


def _model_credentials_and_public_identifiers(*, fresh: bool = True) -> Tuple[set[str], set[str]]:
    global _model_secret_cache
    from core.services.model_config import ModelConfigService

    version = ModelConfigService.get_instance().version
    if not fresh:
        with _effective_lock:
            hit = _model_secret_cache
        if hit is not None and hit[0] == version:
            return hit[1], hit[2]
    found: set[str] = set()
    public_identifiers: set[str] = set()
    with SessionLocal() as db:
        for provider_id, display_name, model_name, api_key, base_url, extra_config in db.query(
            ModelProvider.provider_id,
            ModelProvider.display_name,
            ModelProvider.model_name,
            ModelProvider.api_key,
            ModelProvider.base_url,
            ModelProvider.extra_config,
        ).all():
            public_identifiers.update(
                str(v).strip() for v in (provider_id, display_name, model_name) if v
            )
            found.update(
                _secrets_from_config(
                    {"api_key": api_key, "base_url": base_url, "extra_config": extra_config},
                    model_config=True,
                )
            )
    with _effective_lock:
        _model_secret_cache = (version, found, public_identifiers)
    return found, public_identifiers


def _known_cloud_secrets(user_id: str, *, fresh: bool = True) -> set[str]:
    """Read actual authorized connection credentials into this request only.

    Published content (manifests, bundles) is checked against a fresh read.
    Gateway streams reuse the same 30s authorization snapshot the gateway itself
    resolved the target from.
    """
    try:
        keys, _enabled, configs = _user_capability_configs(user_id, use_cache=not fresh)
        found: set[str] = set()
        for key in keys:
            found.update(_secrets_from_config(configs.get(key) or {}))
        model_secrets, public_identifiers = _model_credentials_and_public_identifiers(fresh=fresh)
        return _without_public_identifiers(found | model_secrets, public_identifiers)
    except Exception:
        raise CapabilityContentRejected() from None


def gateway_stream_secrets(user_id: str, target: Dict[str, Any]) -> set[str]:
    """网关转发上游模型/MCP 输出时要屏蔽的凭据：已授权连接的凭据 + 本次目标自身的凭据，
    同样排除与模型公开标识相同的值（否则每个流式分片里的 model 字段都会命中）。"""
    try:
        _, public_identifiers = _model_credentials_and_public_identifiers(fresh=False)
        return _without_public_identifiers(
            _known_cloud_secrets(user_id, fresh=False) | _secrets_from_config(target),
            public_identifiers,
        )
    except CapabilityContentRejected:
        raise
    except Exception:
        raise CapabilityContentRejected() from None


def _secret_bytes(secrets: set[str]) -> set[bytes]:
    values = set()
    for secret in secrets:
        if secret:
            values.add(secret.encode("utf-8"))
            values.add(json.dumps(secret, ensure_ascii=True)[1:-1].encode("ascii"))
    return values


def _guard_value(value: Any, secrets: set[str]) -> None:
    if isinstance(value, str):
        if any(secret in value for secret in secrets):
            raise CapabilityContentRejected()
    elif isinstance(value, bytes):
        if any(secret in value for secret in _secret_bytes(secrets)):
            raise CapabilityContentRejected()
    elif isinstance(value, dict):
        for key, item in value.items():
            _guard_value(key, secrets)
            _guard_value(item, secrets)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _guard_value(item, secrets)


def guard_capability_content(user_id: str, value: Any, *, extra_secrets: Optional[set[str]] = None):
    _guard_value(value, _known_cloud_secrets(user_id) | (extra_secrets or set()))
    return value


def guard_capability_bundle(user_id: str, resolved):
    """Inspect the final ZIP bytes, so a file changed during packing is caught."""
    if resolved is None:
        return None
    import io
    import zipfile

    data, _revision = resolved
    secrets = _known_cloud_secrets(user_id)
    needles = _secret_bytes(secrets)
    keep = max((len(value) for value in needles), default=1) - 1
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            _guard_value(info.filename, secrets)
            with archive.open(info) as file:
                tail = b""
                while chunk := file.read(64 * 1024):
                    combined = tail + chunk
                    if any(value in combined for value in needles):
                        raise CapabilityContentRejected()
                    tail = combined[-keep:] if keep else b""
    return resolved


async def guard_capability_stream(chunks, secrets: set[str]):
    """Keep enough bytes to detect a credential split at any transport boundary.

    A needle never contains a newline, so nothing can straddle one: every
    complete line is released the moment it was checked. SSE frames end in a
    newline, which keeps token streaming at zero added delay.
    """
    needles = _secret_bytes(secrets)
    keep = max((len(value) for value in needles), default=1) - 1
    line_safe = all(b"\n" not in value for value in needles)
    tail = b""
    async for chunk in chunks:
        combined = tail + chunk
        if any(value in combined for value in needles):
            raise CapabilityContentRejected()
        cut = len(combined) - keep
        if line_safe:
            cut = max(cut, combined.rfind(b"\n") + 1)
        if cut > 0:
            yield combined[:cut]
            tail = combined[cut:]
        else:
            tail = combined
    if tail:
        yield tail


# ── 用户有效能力解析（manifest 与网关共用，30s per-user 缓存） ──────────
def invalidate_model_gateway_cache() -> None:
    """Forget the process-level credential snapshot (a different database was bound)."""
    global _model_secret_cache
    with _effective_lock:
        _model_secret_cache = None
