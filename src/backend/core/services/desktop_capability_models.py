"""Credential-free model topology and cloud model gateway authorization."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.db.engine import SessionLocal
from core.db.models import ModelProvider, ModelRoleAssignment
from core.services.desktop_capability_credentials import CapabilityContentRejected
from core.services.desktop_capability_protocol import canonical_hash
from core.services.desktop_capability_security import _guard_value, _known_cloud_secrets

logger = logging.getLogger(__name__)

# ── 模型清单 / 网关目标（云端真实凭据永不离开本进程） ─────────────────────

_MODEL_PATHS = {
    "chat": "chat/completions",
    "embedding": "embeddings",
    "reranker": "rerank",
}


def _upstream_model_path(provider: ModelProvider) -> Optional[str]:
    """上游该走哪个路径。

    聊天模型有两种线上协议，走哪一种是 ``protocol_probe`` 在配置时探明、写进
    ``extra_config.api_protocol`` 的事实。网关必须照同一份事实转发：本机端按该事实
    调用 ``/responses``，网关却一律改投 ``/chat/completions``，上游就会 404，本机端
    每一轮都先失败一次再回退到别的模型——用户看到的是"本机比云端慢一截"。
    """
    provider_type = str(provider.provider_type or "")
    if provider_type != "chat":
        return _MODEL_PATHS.get(provider_type)
    from core.llm.chat_models import wants_responses
    from core.llm.providers.protocol_probe import PROTOCOL_RESPONSES
    from core.llm.providers.registry import get_spec

    spec = get_spec(getattr(provider, "provider", None) or "openai_compatible")
    protocol = (provider.extra_config or {}).get("api_protocol")
    if wants_responses(spec, protocol):
        return PROTOCOL_RESPONSES
    return _MODEL_PATHS["chat"]


_SENSITIVE_EXTRA_KEY_PARTS = (
    "api_key",
    "access_key",
    "private_key",
    "secret",
    "password",
    "credential",
    "token",
)


def _model_is_gateway_compatible(provider: ModelProvider) -> bool:
    """桌面模型网关当前承载 OpenAI-compatible 三类协议。

    Azure 会由 SDK 重写 deployment 路径与鉴权头，原生 Anthropic /
    Gemini / Bedrock 也不是同一线上协议；在专用适配器完成前不把它们
    伪装成可用，更不会为了兼容而下发真实凭据。
    """
    from core.llm.providers.registry import get_spec

    provider_id = getattr(provider, "provider", None) or "openai_compatible"
    spec = get_spec(provider_id)
    return spec.engine == "openai" and spec.id != "azure_openai"


def _sanitize_model_extra(value: Any) -> Any:
    """递归剔除 extra_config 里可能的凭据，保留上下文长度等运行参数。"""
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if any(part in normalized for part in _SENSITIVE_EXTRA_KEY_PARTS):
                continue
            cleaned[str(key)] = _sanitize_model_extra(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_model_extra(item) for item in value]
    return value


def build_user_model_manifest(user_id: str) -> Dict[str, Any]:
    """返回可安全下发桌面的模型拓扑，不含上游 URL 或任何密钥。

    清单包含全部模型行，使本机旧数据库里曾同步过的明文凭据也会被
    网关占位值覆盖。当前网关不兼容的厂商会下发为 inactive，防止本机
    误调或回落到旧凭据。
    """
    # 用户身份已由 capability token 验证；模型拓扑是全局配置。凭据集合按用户读取，
    # 与出口守卫使用同一来源。
    secrets = _known_cloud_secrets(user_id)
    with SessionLocal() as db:
        providers = db.query(ModelProvider).order_by(ModelProvider.created_at.desc()).all()
        assignments = db.query(ModelRoleAssignment).all()
        rows = []
        withheld = []
        for p in providers:
            row = {
                "provider_id": p.provider_id,
                "display_name": p.display_name,
                "provider_type": p.provider_type,
                "provider": getattr(p, "provider", None) or "openai_compatible",
                "model_name": p.model_name,
                "gateway_group": getattr(p, "gateway_group", None),
                "weight": getattr(p, "weight", 1),
                "priority": getattr(p, "priority", 0),
                "extra_config": _sanitize_model_extra(p.extra_config or {}),
                "is_active": bool(p.is_active and _model_is_gateway_compatible(p)),
            }
            collisions = _credential_collisions(row, secrets)
            if collisions:
                # 某个公开字段（如 model_name）与一条已配置的凭据字面相同：下发它就等于
                # 泄漏凭据。只扣留这一条并点名字段，其余模型照常下发；管理员据此改配置。
                withheld.append({"provider_id": p.provider_id, "fields": collisions})
                logger.warning(
                    "[desktop-capability] model provider withheld from manifest: "
                    "provider_id=%s fields=%s collide with a configured credential",
                    p.provider_id,
                    collisions,
                )
                continue
            rows.append(row)
        provider_ids = {row["provider_id"] for row in rows}
        role_rows = [
            {"role_key": a.role_key, "provider_id": a.provider_id}
            for a in assignments
            if a.provider_id in provider_ids
        ]
    manifest = {"version": 1, "providers": rows, "role_assignments": role_rows}
    if withheld:
        manifest["withheld"] = withheld
    manifest["revision"] = canonical_hash(manifest)
    return manifest


def _credential_collisions(row: Dict[str, Any], secrets: set[str]) -> List[str]:
    """返回模型行里与已配置凭据字面相撞的字段名（不含值）。"""
    fields: List[str] = []
    for field, value in row.items():
        try:
            _guard_value(value, secrets)
        except CapabilityContentRejected:
            fields.append(field)
    return fields


def _model_provider_allowed(db, user_id: str, provider: ModelProvider) -> bool:  # noqa: ANN001
    """角色模型对所有用户可用；额外对话模型受用户切换能力控制。"""
    assigned = (
        db.query(ModelRoleAssignment)
        .filter(ModelRoleAssignment.provider_id == provider.provider_id)
        .first()
    )
    if assigned is not None:
        return True
    if provider.provider_type != "chat":
        return False
    from core.services.user_model_selection import user_can_switch_model

    return user_can_switch_model(db, str(user_id))


def resolve_model_gateway_target(user_id: str, provider_id: str) -> Optional[dict]:
    """解析并授权一个模型上游目标；未授权/不兼容统一返回 None。

    每次现查：用户的模型切换权限没有变更信号，缓存会让撤权延迟生效。
    调用方必须在线程池里执行，不得占住事件循环。
    """
    pid = str(provider_id or "").strip()
    if not pid:
        return None
    with SessionLocal() as db:
        provider = (
            db.query(ModelProvider)
            .filter(
                ModelProvider.provider_id == pid,
                ModelProvider.is_active == True,  # noqa: E712
            )
            .first()
        )
        if provider is None or not _model_is_gateway_compatible(provider):
            return None
        path = _upstream_model_path(provider)
        base_url = str(provider.base_url or "").strip().rstrip("/")
        if not path or not base_url or not _model_provider_allowed(db, user_id, provider):
            return None
        return {
            "url": f"{base_url}/{path}",
            "api_key": str(provider.api_key or ""),
            "model_name": str(provider.model_name or ""),
            "provider_type": str(provider.provider_type),
            "path": path,
        }
