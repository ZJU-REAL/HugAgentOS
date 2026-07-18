"""Per-user API-Key management API.

GET    /v1/me/api-keys                List the current user's API-Keys
POST   /v1/me/api-keys                Create (plaintext returned only this once)
GET    /v1/me/api-keys/{key_id}/reveal Retrieve the full plaintext again (decrypt key_enc, for copying)
PATCH  /v1/me/api-keys/{key_id}        Enable / disable
DELETE /v1/me/api-keys/{key_id}        Revoke

Available only when the user's capability bit ``can_use_api_key=true``, otherwise 403. This
switch is controlled by the user-management module of the Config admin platform.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import get_current_user, UserContext
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError, ResourceNotFoundError
from core.infra.responses import success_response, created_response
from core.licensing import Feature, license_manager
from core.services import ApiKeyService

router = APIRouter(prefix="/v1/me/api-keys", tags=["API Keys"])

# Expiry options (days). None means never expires. The frontend dropdown renders based on this.
ALLOWED_EXPIRY_DAYS = {7, 30, 90, 180, 365}


class CreateApiKeyRequest(BaseModel):
    name: str = Field("API Key", max_length=128, description="便于识别的名称")
    expires_in_days: Optional[int] = Field(
        None, description="过期天数，留空=永不过期；允许 7/30/90/180/365"
    )
    for_gateway: bool = Field(
        False, description="同时把此密钥用于对外模型网关（Cherry Studio 等可直接用它调用）"
    )


class ToggleApiKeyRequest(BaseModel):
    enabled: bool


def _require_api_key_permission(user_id: str, db: Session) -> None:
    """Check whether the user is allowed to use API-Keys: personal explicit → team default → off by default."""
    if not resolve_user_capabilities(db, user_id)["can_use_api_key"]:
        raise AccessDeniedError(
            message="管理员未开放 API-Key 功能",
            reason="api_key_disabled",
        )


def _gateway_service_or_none(db: Session):
    """Return the EE gateway service only when the feature is enabled."""
    if not license_manager.has(Feature.MODEL_GATEWAY):
        return None
    from core.services.litellm_gateway_service import LiteLLMGatewayService

    return LiteLLMGatewayService(db)


def _key_to_dict(row, *, plaintext: Optional[str] = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "key_prefix": row.key_prefix,
        "enabled": row.enabled,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        # Whether the plaintext can be retrieved again (yes if ciphertext exists; old keys without ciphertext cannot) — the frontend uses this to decide whether to show "Copy"
        "revealable": bool(getattr(row, "key_enc", None)),
        # Plaintext is returned only on create/reveal; always None in listings
        "api_key": plaintext,
    }


@router.get("", summary="API-Key 列表")
async def list_api_keys(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出当前用户全部未撤销的 API-Key（不含明文）。"""
    _require_api_key_permission(str(user.user_id), db)
    rows = ApiKeyService(db).list_keys(str(user.user_id))
    return success_response(data={"items": [_key_to_dict(r) for r in rows]})


@router.post("", status_code=status.HTTP_201_CREATED, summary="新建 API-Key")
async def create_api_key(
    body: CreateApiKeyRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """生成一个新的 API-Key。明文 ``api_key`` 仅在本次响应返回，请妥善保存。"""
    _require_api_key_permission(str(user.user_id), db)

    expires_in_days = body.expires_in_days
    if expires_in_days is not None and expires_in_days not in ALLOWED_EXPIRY_DAYS:
        from core.infra.exceptions import BadRequestError

        raise BadRequestError(
            message="无效的过期天数",
            data={"allowed": sorted(ALLOWED_EXPIRY_DAYS)},
        )

    gw = None
    if body.for_gateway:
        from core.infra.exceptions import BadRequestError

        license_manager.require(Feature.MODEL_GATEWAY)
        gw = _gateway_service_or_none(db)
        if gw is None:
            raise BadRequestError(message="对外模型网关未授权，无法将密钥用于网关")
        if not gw.is_configured():
            raise BadRequestError(message="对外模型网关未启用，无法将密钥用于网关")

    row, raw = ApiKeyService(db).create_key(
        user_id=str(user.user_id),
        name=body.name,
        expires_in_days=expires_in_days,
    )

    # Optional: also register this key into the outbound model gateway (plaintext is only available at this moment, so it can only be included at creation time).
    if gw is not None:
        await gw.register_user_key(
            raw, user_api_key_id=row.id,
            owner=getattr(user, "username", None) or str(user.user_id),
            display_name=f"用户密钥：{row.name}",
        )

    data = _key_to_dict(row, plaintext=raw)
    data["for_gateway"] = bool(body.for_gateway)
    return created_response(data=data)


@router.get("/{key_id}/reveal", summary="再次取回 API-Key 明文")
async def reveal_api_key(
    key_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """解密并返回某个 API-Key 的完整明文，供前端「再次复制」。

    旧密钥（本能力上线前创建、无密文）无法找回明文，返回 410 提示用户新建。
    """
    _require_api_key_permission(str(user.user_id), db)
    row = ApiKeyService(db).get_key(str(user.user_id), key_id)
    if not row:
        raise ResourceNotFoundError("api_key", key_id)

    if not row.key_enc:
        from core.infra.exceptions import BadRequestError

        raise BadRequestError(
            message="该密钥创建于「再次复制」功能上线前，明文无法找回，请撤销后新建",
            data={"reason": "api_key_not_revealable"},
        )

    from core.infra.crypto import decrypt_secret

    plaintext = decrypt_secret(row.key_enc)
    if not plaintext:
        # Ciphertext exists but cannot be decrypted (usually a deployment key change) — treat as unrecoverable
        from core.infra.exceptions import BadRequestError

        raise BadRequestError(
            message="密钥明文解密失败（部署密钥可能已变更），请撤销后新建",
            data={"reason": "api_key_decrypt_failed"},
        )

    return success_response(data=_key_to_dict(row, plaintext=plaintext))


@router.patch("/{key_id}", summary="启用/禁用 API-Key")
async def toggle_api_key(
    key_id: str,
    body: ToggleApiKeyRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """切换某个 API-Key 的启用状态。"""
    _require_api_key_permission(str(user.user_id), db)
    row = ApiKeyService(db).set_enabled(str(user.user_id), key_id, body.enabled)
    if not row:
        raise ResourceNotFoundError("api_key", key_id)
    # If this key is registered in the gateway, cascade block/unblock (no-op if not registered)
    gw = _gateway_service_or_none(db)
    if gw is not None:
        await gw.set_user_key_active(key_id, body.enabled)
    return success_response(data=_key_to_dict(row))


@router.delete("/{key_id}", summary="撤销 API-Key")
async def revoke_api_key(
    key_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """撤销（软删除）某个 API-Key，撤销后立即失效且不可恢复。"""
    _require_api_key_permission(str(user.user_id), db)
    ok = ApiKeyService(db).revoke_key(str(user.user_id), key_id)
    if not ok:
        raise ResourceNotFoundError("api_key", key_id)
    # Cascade-delete the gateway-side mirror (no-op if not registered)
    gw = _gateway_service_or_none(db)
    if gw is not None:
        await gw.unregister_user_key(key_id)
    return success_response(data={"id": key_id, "revoked": True})
