"""Model management API routes.

Provides CRUD for model providers and role assignments,
plus connectivity testing and export/import.

Admin endpoint gate = ``require_system_settings`` (CONFIG_TOKEN / can_system_config capability bit /
CE+mock single trust domain): under EE it matches the semantics of the former ``require_config``
(the same set of authorized users); under CE it lets the instance administrator configure models
directly in the Web "Settings → Model Services" page — model configuration is purely DB-driven,
and without this delegation a CE deployment boots as dummy-model. The pricing fields depend on
``model_pricing`` (EE_ONLY_TABLES) and are skipped entirely under CE (see ``_pricing_enabled``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.deps import require_config, require_system_settings
from core.auth.backend import UserContext, require_auth
from core.config.settings import settings
from core.db.engine import get_db
from core.services.model_config import ModelConfigService
from core.db.model_repository import (
    ROLE_DEFINITIONS,
    assign_role,
    create_provider,
    delete_provider,
    export_all,
    get_provider,
    import_all,
    list_providers,
    list_role_assignments,
    provider_is_referenced,
    set_provider_test_result,
    unassign_role,
    update_provider,
)
from core.infra.responses import success_response
from core.services.user_model_selection import list_user_selectable_models, user_can_switch_model

router = APIRouter(prefix="/v1/models", tags=["Models"])
logger = logging.getLogger(__name__)


# ── Request / response schemas ────────────────────────────────────────────────


class ProviderCreateRequest(BaseModel):
    display_name: str
    provider_type: str = Field(..., pattern="^(chat|embedding|reranker)$")
    provider: str = "openai_compatible"  # vendor/protocol, see core/llm/providers/registry.py
    base_url: str = ""
    api_key: str = ""
    model_name: str
    extra_config: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    # External gateway "model group": multiple providers sharing the same gateway_group are merged
    # at the gateway into multiple upstreams behind one external alias (activating LiteLLM load
    # balancing/failover); empty = standalone single upstream. weight = weighted round-robin weight within the pool.
    gateway_group: Optional[str] = None
    weight: int = Field(1, ge=1)
    priority: int = Field(0, ge=0)
    # Billing unit price (¥/1K tokens), written to the shared model_pricing table, so usage stats / token billing look up prices by model_name
    input_price: Optional[float] = Field(None, ge=0)
    output_price: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = None


class ProviderUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    provider_type: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    extra_config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    gateway_group: Optional[str] = None  # empty string = clear the group (revert to standalone single upstream)
    weight: Optional[int] = Field(None, ge=1)
    priority: Optional[int] = Field(None, ge=0)
    input_price: Optional[float] = Field(None, ge=0)
    output_price: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = None


class TestConnectionRequest(BaseModel):
    """For testing a provider config that hasn't been saved yet."""

    provider_type: str = Field(..., pattern="^(chat|embedding|reranker)$")
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model_name: str
    extra_config: Dict[str, Any] = Field(default_factory=dict)


class RoleAssignRequest(BaseModel):
    provider_id: str


class ImportRequest(BaseModel):
    providers: List[Dict[str, Any]] = Field(default_factory=list)
    role_assignments: List[Dict[str, Any]] = Field(default_factory=list)
    overwrite: bool = True


# ── Helpers ───────────────────────────────────────────────────────────────────


def _mask_api_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def _provider_to_dict(p, pricing=None) -> dict:
    return {
        "provider_id": p.provider_id,
        "display_name": p.display_name,
        "provider_type": p.provider_type,
        "provider": getattr(p, "provider", "openai_compatible"),
        "base_url": p.base_url,
        "api_key": _mask_api_key(p.api_key),
        "model_name": p.model_name,
        "extra_config": p.extra_config or {},
        "is_active": p.is_active,
        "gateway_group": getattr(p, "gateway_group", None) or None,
        "weight": getattr(p, "weight", 1) or 1,
        "priority": getattr(p, "priority", 0) or 0,
        # Billing unit price (from the shared model_pricing table, joined by model_name; null when unconfigured)
        "input_price": float(pricing.input_price) if pricing else None,
        "output_price": float(pricing.output_price) if pricing else None,
        "currency": pricing.currency if pricing else "CNY",
        "last_tested_at": p.last_tested_at.isoformat() if p.last_tested_at else None,
        "last_test_status": p.last_test_status,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _pricing_enabled() -> bool:
    """The pricing table ``model_pricing`` belongs to ``EE_ONLY_TABLES`` — the CE derived tree doesn't create it.

    Under CE all pricing reads/writes are skipped (input_price/output_price are always null in
    responses); otherwise, once the full models.py lands in CE, the very first list request would
    error because the table doesn't exist.
    """
    return settings.edition.edition != "ce"


def _pricing_map(db) -> dict:
    """model_name → ModelPricing, fetched all at once for batch joining in the list (avoids per-provider N+1 queries)."""
    if not _pricing_enabled():
        return {}
    from core.db.models import ModelPricing

    return {r.model_name: r for r in db.query(ModelPricing).all()}


def _get_pricing(db, model_name: str):
    if not _pricing_enabled():
        return None
    from core.db.models import ModelPricing

    return db.query(ModelPricing).filter(ModelPricing.model_name == model_name).first()


def _upsert_pricing(
    db,
    *,
    model_name: str,
    input_price: Optional[float] = None,
    output_price: Optional[float] = None,
    currency: Optional[str] = None,
    display_name: Optional[str] = None,
) -> None:
    """Upsert one model_pricing row by model_name. Providers sharing the same upstream model_name share the unit price.

    Only overwrite explicitly given fields; on creation the default price is 0 and currency CNY.
    display_name is filled in only when previously empty. Under CE (no model_pricing table),
    silently skip.
    """
    if not _pricing_enabled():
        return
    from core.db.models import ModelPricing

    row = db.query(ModelPricing).filter(ModelPricing.model_name == model_name).first()
    if row is None:
        row = ModelPricing(
            pricing_id=f"mp_{uuid.uuid4().hex[:16]}",
            model_name=model_name,
            display_name=display_name,
            input_price=input_price or 0,
            output_price=output_price or 0,
            currency=currency or "CNY",
        )
        db.add(row)
    else:
        if input_price is not None:
            row.input_price = input_price
        if output_price is not None:
            row.output_price = output_price
        if currency is not None:
            row.currency = currency
        if display_name and not row.display_name:
            row.display_name = display_name
    db.commit()


def _normalize_base_url(
    base_url: str, provider_type: str, provider: str = "openai_compatible"
) -> str:
    """Normalize base_url.

    Only append the '/v1' suffix for the **generic** openai_compatible vendor (users often omit it);
    all other vendors (domestic vendors with preset base_urls, e.g. Zhipu uses /v4; Azure;
    Anthropic/Gemini/DashScope/Ollama native; litellm) are left untouched — their base_url shapes
    vary and '/v1' must not be force-appended.
    """
    url = (base_url or "").strip().rstrip("/")
    if provider != "openai_compatible":
        return url
    if provider_type in ("chat", "embedding") and url and not url.endswith("/v1"):
        url = url + "/v1"
    return url


async def _validate_provider_config(
    base_url: str,
    api_key: str,
    model_name: str,
    provider_type: str,
    provider: str = "openai_compatible",
    extra_config: Optional[dict] = None,
) -> None:
    """Validate provider config at save time. Raises HTTPException on failure."""
    normalized_url = _normalize_base_url(base_url, provider_type, provider)
    result = await _test_connection(
        provider,
        provider_type,
        normalized_url,
        api_key,
        model_name,
        extra_config or {},
    )
    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=f"模型连通性验证失败：{result['error']}。请检查配置（URL / 令牌 / 模型名 / 厂商凭据）是否正确。",
        )


async def _http_ping(url: str, headers: dict, payload: dict, timeout: int = 15) -> dict:
    """Timed POST + status judgment; returns {success, latency_ms, error}."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            return {"success": True, "latency_ms": latency, "error": None}
        return {
            "success": False,
            "latency_ms": latency,
            "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except Exception as exc:
        latency = int((time.monotonic() - start) * 1000)
        return {"success": False, "latency_ms": latency, "error": str(exc)}


async def _ping_openai_compat(
    provider_type: str, base_url: str, api_key: str, model_name: str
) -> dict:
    base_url = base_url.rstrip("/")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    if provider_type == "chat":
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
        }
    elif provider_type == "embedding":
        url = f"{base_url}/embeddings"
        payload = {"model": model_name, "input": "test"}
    elif provider_type == "reranker":
        url = f"{base_url}/rerank"
        payload = {"model": model_name, "query": "test", "documents": ["a", "b"]}
    else:
        return {"success": False, "latency_ms": 0, "error": f"Unknown type: {provider_type}"}
    return await _http_ping(url, headers, payload)


async def _ping_azure(base_url: str, api_key: str, extra: dict) -> dict:
    deployment = (extra or {}).get("deployment", "")
    api_version = (extra or {}).get("api_version", "")
    if not deployment or not api_version:
        return {"success": False, "latency_ms": 0, "error": "Azure 需要 deployment 与 api_version"}
    url = f"{base_url.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    headers = {"Content-Type": "application/json", "api-key": api_key}
    payload = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    return await _http_ping(url, headers, payload)


async def _ping_via_model(
    provider: str, base_url: str, api_key: str, model_name: str, extra: dict
) -> dict:
    """Native/litellm vendors: actually build the model and issue one minimal non-streaming call to verify connectivity."""
    from agentscope.message import Msg, TextBlock

    from core.llm.chat_models import make_chat_model
    from core.llm.providers.registry import get_spec, split_provider_extra

    spec = get_spec(provider)
    provider_extra = split_provider_extra(spec, extra or {})
    start = time.monotonic()
    try:
        model = make_chat_model(
            model=model_name,
            temperature=0.0,
            max_tokens=16,
            timeout=30,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
            provider_extra=provider_extra,
            stream=False,
            # Connectivity test: the candidate config may not be persisted yet (so the window
            # can't be resolved by model name), and the ping never enters the compaction loop —
            # context_size takes part in no computation. Prefer the context_length entered in the
            # form, otherwise use a nominal value.
            context_size=int((extra or {}).get("context_length") or 0) or 4096,
        )
        ping_msg = Msg(name="user", content=[TextBlock(type="text", text="hi")], role="user")
        await asyncio.wait_for(model([ping_msg]), timeout=30)
        latency = int((time.monotonic() - start) * 1000)
        return {"success": True, "latency_ms": latency, "error": None}
    except Exception as exc:
        latency = int((time.monotonic() - start) * 1000)
        return {"success": False, "latency_ms": latency, "error": str(exc)[:300]}


async def _test_connection(
    provider: str,
    provider_type: str,
    base_url: str,
    api_key: str,
    model_name: str,
    extra_config: Optional[dict] = None,
) -> dict:
    """Test connectivity (dispatched by engine). Returns {success, latency_ms, error}."""
    from core.llm.providers.registry import get_spec

    spec = get_spec(provider)
    extra = extra_config or {}
    if spec.engine == "openai":
        if spec.id == "azure_openai":
            return await _ping_azure(base_url, api_key, extra)
        return await _ping_openai_compat(provider_type, base_url, api_key, model_name)
    # native / litellm
    return await _ping_via_model(provider, base_url, api_key, model_name, extra)


# ── Public capabilities endpoint ──────────────────────────────────────────────


@router.get("/capabilities", summary="主模型能力（公开端点，供前端开关）")
async def get_main_capabilities(
    db: Session = Depends(get_db),
    user: Optional[UserContext] = Depends(require_auth(False)),
):
    """供前端展示模型相关入口；只暴露非敏感信息，不泄露 base_url/api_key。"""
    cfg = ModelConfigService.get_instance().resolve("main_agent")
    supports = bool((cfg.extra if cfg else {}).get("supports_reasoning_effort"))
    switch_enabled = bool(user and user_can_switch_model(db, user.user_id))
    return success_response(
        data={
            "main_agent": {"supports_reasoning_effort": supports},
            "user_model_switch": {
                "enabled": switch_enabled,
                "models": list_user_selectable_models(db) if switch_enabled else [],
            },
        }
    )


# ── Provider schema (vendor field definitions, driving the frontend dynamic form) ──


@router.get("/provider-schemas", summary="列出所有厂商及其字段定义")
async def list_provider_schemas(_: None = Depends(require_system_settings)):
    """返回各 provider（厂商/协议）的引擎、支持用途、base_url 模板与厂商特有字段。

    供前端模型配置表单按所选厂商动态渲染。仅限管理员。
    """
    from core.llm.providers.registry import to_frontend_schema

    return success_response(data=to_frontend_schema())


# ── Provider endpoints ────────────────────────────────────────────────────────


@router.get("/providers", summary="列出所有模型供应商")
async def list_providers_endpoint(
    _: None = Depends(require_system_settings),
    db: Session = Depends(get_db),
):
    """列出所有已配置的模型供应商（chat/embedding/reranker）。仅限管理员；返回的 api_key 已脱敏。"""
    providers = list_providers(db)
    pm = _pricing_map(db)
    return success_response(data=[_provider_to_dict(p, pm.get(p.model_name)) for p in providers])


@router.post("/providers", summary="新增模型供应商")
async def create_provider_endpoint(
    body: ProviderCreateRequest,
    _: None = Depends(require_system_settings),
    db: Session = Depends(get_db),
):
    """新增一个模型供应商。仅限管理员；保存前会校验厂商字段并做连通性预校验，失败则返回 400，成功后刷新模型配置缓存。"""
    from core.llm.providers.registry import validate_payload

    err = validate_payload(body.provider, body.provider_type, body.extra_config)
    if err:
        raise HTTPException(status_code=400, detail=err)

    normalized_url = _normalize_base_url(body.base_url, body.provider_type, body.provider)
    await _validate_provider_config(
        normalized_url,
        body.api_key,
        body.model_name,
        body.provider_type,
        provider=body.provider,
        extra_config=body.extra_config,
    )

    provider = create_provider(
        db,
        display_name=body.display_name,
        provider_type=body.provider_type,
        provider=body.provider,
        base_url=normalized_url,
        api_key=body.api_key,
        model_name=body.model_name,
        gateway_group=body.gateway_group,
        weight=body.weight,
        priority=body.priority,
        extra_config=body.extra_config,
        is_active=body.is_active,
    )
    given = body.model_dump(exclude_unset=True)
    if any(k in given for k in ("input_price", "output_price", "currency")):
        _upsert_pricing(
            db,
            model_name=provider.model_name,
            input_price=body.input_price,
            output_price=body.output_price,
            currency=body.currency,
            display_name=body.display_name,
        )
    ModelConfigService.get_instance().invalidate_cache()
    return success_response(data=_provider_to_dict(provider, _get_pricing(db, provider.model_name)))


@router.put("/providers/{provider_id}", summary="更新模型供应商")
async def update_provider_endpoint(
    provider_id: str,
    body: ProviderUpdateRequest,
    _: None = Depends(require_system_settings),
    db: Session = Depends(get_db),
):
    """更新指定供应商的配置（仅传需修改的字段）。仅限管理员；若改动了 base_url/api_key/模型名会重新做连通性校验，成功后刷新模型配置缓存；供应商不存在返回 404。"""
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    # Pricing fields are not provider columns; extract them separately and upsert into model_pricing
    price_fields = {
        k: fields.pop(k) for k in ("input_price", "output_price", "currency") if k in fields
    }

    # If URL or credentials changed, validate the new config
    existing = get_provider(db, provider_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    new_url = fields.get("base_url", existing.base_url)
    new_key = fields.get("api_key", existing.api_key)
    new_model = fields.get("model_name", existing.model_name)
    new_type = fields.get("provider_type", existing.provider_type)
    new_provider = fields.get("provider", getattr(existing, "provider", "openai_compatible"))
    new_extra = fields.get("extra_config", existing.extra_config or {})

    from core.llm.providers.registry import validate_payload

    err = validate_payload(new_provider, new_type, new_extra)
    if err:
        raise HTTPException(status_code=400, detail=err)

    if any(k in fields for k in ("base_url", "api_key", "model_name", "provider", "extra_config")):
        normalized_url = _normalize_base_url(new_url, new_type, new_provider)
        await _validate_provider_config(
            normalized_url,
            new_key,
            new_model,
            new_type,
            provider=new_provider,
            extra_config=new_extra,
        )
        fields["base_url"] = normalized_url

    provider = update_provider(db, provider_id, **fields)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if price_fields:
        _upsert_pricing(
            db,
            model_name=provider.model_name,
            display_name=provider.display_name,
            **price_fields,
        )
    ModelConfigService.get_instance().invalidate_cache()
    return success_response(data=_provider_to_dict(provider, _get_pricing(db, provider.model_name)))


@router.delete("/providers/{provider_id}", summary="删除模型供应商")
async def delete_provider_endpoint(
    provider_id: str,
    _: None = Depends(require_system_settings),
    db: Session = Depends(get_db),
):
    """删除指定供应商。仅限管理员；若仍被某些角色引用则返回 409 并列出引用角色，删除成功后刷新模型配置缓存；不存在返回 404。"""
    refs = provider_is_referenced(db, provider_id)
    if refs:
        raise HTTPException(
            status_code=409,
            detail=f"该供应商正被以下角色引用，请先取消分配：{', '.join(refs)}",
        )
    if not delete_provider(db, provider_id):
        raise HTTPException(status_code=404, detail="Provider not found")
    ModelConfigService.get_instance().invalidate_cache()
    return success_response(data={"deleted": provider_id})


# ── Connectivity testing ──────────────────────────────────────────────────────


@router.post("/providers/{provider_id}/test", summary="测试已保存供应商连通性")
async def test_saved_provider(
    provider_id: str,
    _: None = Depends(require_system_settings),
    db: Session = Depends(get_db),
):
    """对已保存的供应商发起一次实际连通性测试。仅限管理员；会把测试结果（成功/失败）回写到供应商记录，并返回耗时与错误信息；不存在返回 404。"""
    provider = get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    prov = getattr(provider, "provider", "openai_compatible")
    test_url = _normalize_base_url(provider.base_url, provider.provider_type, prov)
    result = await _test_connection(
        prov,
        provider.provider_type,
        test_url,
        provider.api_key,
        provider.model_name,
        provider.extra_config or {},
    )
    set_provider_test_result(db, provider_id, result["success"])
    return success_response(data=result)


@router.post("/providers/test", summary="测试未保存配置连通性（预检）")
async def test_unsaved_provider(
    body: TestConnectionRequest,
    _: None = Depends(require_system_settings),
):
    """对一份尚未保存的供应商配置做连通性预检。仅限管理员；用于新增/编辑表单提交前验证 URL、令牌与模型名，不落库。"""
    test_url = _normalize_base_url(body.base_url, body.provider_type, body.provider)
    result = await _test_connection(
        body.provider,
        body.provider_type,
        test_url,
        body.api_key,
        body.model_name,
        body.extra_config,
    )
    return success_response(data=result)


# ── Role assignment endpoints ─────────────────────────────────────────────────


@router.get("/roles", summary="列出所有角色及当前分配")
async def list_roles_endpoint(
    _: None = Depends(require_system_settings),
    db: Session = Depends(get_db),
):
    """列出所有系统角色（如 main_agent、embedding 等）及其当前分配的供应商。仅限管理员。"""
    return success_response(data=list_role_assignments(db))


@router.put("/roles/{role_key}", summary="为角色分配供应商")
async def assign_role_endpoint(
    role_key: str,
    body: RoleAssignRequest,
    _: None = Depends(require_system_settings),
    db: Session = Depends(get_db),
):
    """把指定供应商分配给某个角色。仅限管理员；会校验供应商类型与角色要求一致（如 chat 角色不能配 embedding 供应商），成功后刷新模型配置缓存；角色或供应商不存在返回 404。"""
    if role_key not in ROLE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role_key}")

    # Type check: ensure provider_type matches role's required type
    provider = get_provider(db, body.provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    required_type = ROLE_DEFINITIONS[role_key]["type"]
    if provider.provider_type != required_type:
        raise HTTPException(
            status_code=400,
            detail=f"角色 '{role_key}' 需要 {required_type} 类型的供应商，但所选供应商是 {provider.provider_type} 类型",
        )

    if not assign_role(db, role_key, body.provider_id):
        raise HTTPException(status_code=400, detail="Assignment failed")
    ModelConfigService.get_instance().invalidate_cache()
    return success_response(data={"role_key": role_key, "provider_id": body.provider_id})


@router.delete("/roles/{role_key}", summary="取消角色分配")
async def unassign_role_endpoint(
    role_key: str,
    _: None = Depends(require_system_settings),
    db: Session = Depends(get_db),
):
    """取消某个角色的供应商分配。仅限管理员；解除后刷新模型配置缓存；角色不存在返回 404。"""
    if role_key not in ROLE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role_key}")
    unassign_role(db, role_key)
    ModelConfigService.get_instance().invalidate_cache()
    return success_response(data={"role_key": role_key, "provider_id": None})


# ── Export / Import ───────────────────────────────────────────────────────────


@router.get("/export", summary="导出模型配置")
async def export_endpoint(
    _: None = Depends(require_config),
    db: Session = Depends(get_db),
):
    """导出全部模型配置（供应商 + 角色分配），用于备份或跨环境迁移。

    ⚠️ 导出内容含**明文 api_key**（供跨环境迁移原样重导入）。与其余管理端点
    （已下放 ``require_system_settings``，CE 用户可增删改供应商）不同，本端点
    仍要求 ``require_config``（CONFIG_TOKEN / can_system_config）——避免 CE 单信任域
    下任意登录用户批量导出所有密钥明文。import 同理。
    """
    return success_response(data=export_all(db))


@router.post("/import", summary="导入模型配置")
async def import_endpoint(
    body: ImportRequest,
    _: None = Depends(require_config),
    db: Session = Depends(get_db),
):
    """导入模型配置（供应商 + 角色分配）。仅限管理员（CONFIG_TOKEN / can_system_config，同 export）；overwrite=True 时覆盖同名条目，导入后刷新模型配置缓存。"""
    result = import_all(db, body.model_dump(), overwrite=body.overwrite)
    ModelConfigService.get_instance().invalidate_cache()
    return success_response(data=result)
