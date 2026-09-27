"""Owner-managed credentials and call metadata for one published sub-agent."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.responses import created_response, paginated_response, success_response
from core.services.agent_api_service import list_agent_api_calls, require_agent_manager
from core.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/v1/agents", tags=["Agent API Keys"])
_ALLOWED_EXPIRY_DAYS = {7, 30, 90, 180, 365}


class CreateAgentApiKeyRequest(BaseModel):
    name: str = Field("API Key", min_length=1, max_length=128)
    expires_in_days: Optional[int] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("expires_in_days")
    @classmethod
    def valid_expiry(cls, value):
        if value is not None and value not in _ALLOWED_EXPIRY_DAYS:
            raise ValueError("expires_in_days must be 7, 30, 90, 180 or 365")
        return value


class ToggleAgentApiKeyRequest(BaseModel):
    enabled: bool


def _manager(
    agent_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not resolve_user_capabilities(db, user.user_id)["can_use_api_key"]:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "api_key_disabled",
                "message": "管理员未开放 API-Key 功能",
            },
        )
    require_agent_manager(db, user.user_id, agent_id)
    return user


def _dto(row, plaintext=None):
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "name": row.name,
        "key_prefix": row.key_prefix,
        "enabled": row.enabled,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "revealable": bool(row.key_enc),
        "api_key": plaintext,
    }


def _find(db, user, agent_id, key_id):
    row = ApiKeyService(db).get_key(user.user_id, key_id, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "api_key_not_found"})
    return row


@router.get("/{agent_id}/api-keys", summary="当前账号的子智能体 API Key")
def list_keys(agent_id: str, user: UserContext = Depends(_manager), db: Session = Depends(get_db)):
    rows = ApiKeyService(db).list_keys(user.user_id, agent_id)
    return success_response(data={"items": [_dto(row) for row in rows]})


@router.post("/{agent_id}/api-keys", status_code=201, summary="申请子智能体专属 API Key")
def create_key(
    agent_id: str,
    body: CreateAgentApiKeyRequest,
    response: Response,
    user: UserContext = Depends(_manager),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    row, raw = ApiKeyService(db).create_key(
        user.user_id,
        body.name,
        body.expires_in_days,
        agent_id=agent_id,
    )
    return created_response(data=_dto(row, raw))


@router.get("/{agent_id}/api-keys/{key_id}/reveal", summary="复制子智能体 API Key")
def reveal_key(
    agent_id: str,
    key_id: str,
    response: Response,
    user: UserContext = Depends(_manager),
    db: Session = Depends(get_db),
):
    from core.infra.crypto import decrypt_secret

    response.headers["Cache-Control"] = "no-store"
    row = _find(db, user, agent_id, key_id)
    raw = decrypt_secret(row.key_enc) if row.key_enc else None
    if not raw:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "api_key_not_revealable",
                "message": "该密钥无法再次复制，请撤销后重新申请",
            },
        )
    return success_response(data=_dto(row, raw))


@router.patch("/{agent_id}/api-keys/{key_id}", summary="启停子智能体 API Key")
def toggle_key(
    agent_id: str,
    key_id: str,
    body: ToggleAgentApiKeyRequest,
    user: UserContext = Depends(_manager),
    db: Session = Depends(get_db),
):
    _find(db, user, agent_id, key_id)
    row = ApiKeyService(db).set_enabled(user.user_id, key_id, body.enabled, agent_id)
    return success_response(data=_dto(row))


@router.delete("/{agent_id}/api-keys/{key_id}", summary="撤销子智能体 API Key")
def revoke_key(
    agent_id: str,
    key_id: str,
    user: UserContext = Depends(_manager),
    db: Session = Depends(get_db),
):
    _find(db, user, agent_id, key_id)
    ApiKeyService(db).revoke_key(user.user_id, key_id, agent_id)
    return success_response(data={"id": key_id, "revoked": True})


@router.get("/{agent_id}/api-calls", summary="当前账号的子智能体 API 调用记录")
def list_calls(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    key_id: Optional[str] = Query(None, max_length=64),
    status: Optional[
        Literal["running", "completed", "failed", "cancelled", "needs_attention"]
    ] = None,
    user: UserContext = Depends(_manager),
    db: Session = Depends(get_db),
):
    items, total = list_agent_api_calls(
        db,
        user.user_id,
        agent_id,
        page=page,
        page_size=page_size,
        key_id=key_id,
        status=status,
    )
    return paginated_response(items, page, page_size, total)
