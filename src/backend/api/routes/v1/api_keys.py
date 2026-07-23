"""Community-edition per-user API-Key management routes.

The enterprise route with the same public contract also integrates with the
optional model gateway.  CE keeps the native agent-call key lifecycle here so
the derived tree has no import or entitlement dependency on ``edition_ee``.
"""

from __future__ import annotations

from typing import Optional

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError, BadRequestError, ResourceNotFoundError
from core.infra.responses import created_response, success_response
from core.services import ApiKeyService
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/me/api-keys", tags=["API Keys"])

ALLOWED_EXPIRY_DAYS = {7, 30, 90, 180, 365}


class CreateApiKeyRequest(BaseModel):
    name: str = Field("API Key", max_length=128, description="便于识别的名称")
    expires_in_days: Optional[int] = Field(
        None,
        description="过期天数，留空=永不过期；允许 7/30/90/180/365",
    )


class ToggleApiKeyRequest(BaseModel):
    enabled: bool


def _require_api_key_permission(user_id: str, db: Session) -> None:
    if not resolve_user_capabilities(db, user_id)["can_use_api_key"]:
        raise AccessDeniedError(
            message="管理员未开放 API-Key 功能",
            reason="api_key_disabled",
        )


def _key_to_dict(row, *, plaintext: Optional[str] = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "key_prefix": row.key_prefix,
        "enabled": row.enabled,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "revealable": bool(getattr(row, "key_enc", None)),
        "api_key": plaintext,
    }


@router.get("", summary="API-Key 列表")
async def list_api_keys(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_api_key_permission(str(user.user_id), db)
    rows = ApiKeyService(db).list_keys(str(user.user_id))
    return success_response(data={"items": [_key_to_dict(row) for row in rows]})


@router.post("", status_code=status.HTTP_201_CREATED, summary="新建 API-Key")
async def create_api_key(
    body: CreateApiKeyRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_api_key_permission(str(user.user_id), db)
    if body.expires_in_days is not None and body.expires_in_days not in ALLOWED_EXPIRY_DAYS:
        raise BadRequestError(
            message="无效的过期天数",
            data={"allowed": sorted(ALLOWED_EXPIRY_DAYS)},
        )

    row, raw = ApiKeyService(db).create_key(
        user_id=str(user.user_id),
        name=body.name,
        expires_in_days=body.expires_in_days,
    )
    return created_response(data=_key_to_dict(row, plaintext=raw))


@router.get("/{key_id}/reveal", summary="再次取回 API-Key 明文")
async def reveal_api_key(
    key_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_api_key_permission(str(user.user_id), db)
    row = ApiKeyService(db).get_key(str(user.user_id), key_id)
    if not row:
        raise ResourceNotFoundError("api_key", key_id)
    if not row.key_enc:
        raise BadRequestError(
            message="该密钥创建于「再次复制」功能上线前，明文无法找回，请撤销后新建",
            data={"reason": "api_key_not_revealable"},
        )

    from core.infra.crypto import decrypt_secret

    plaintext = decrypt_secret(row.key_enc)
    if not plaintext:
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
    _require_api_key_permission(str(user.user_id), db)
    row = ApiKeyService(db).set_enabled(str(user.user_id), key_id, body.enabled)
    if not row:
        raise ResourceNotFoundError("api_key", key_id)
    return success_response(data=_key_to_dict(row))


@router.delete("/{key_id}", summary="撤销 API-Key")
async def revoke_api_key(
    key_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_api_key_permission(str(user.user_id), db)
    if not ApiKeyService(db).revoke_key(str(user.user_id), key_id):
        raise ResourceNotFoundError("api_key", key_id)
    return success_response(data={"id": key_id, "revoked": True})
