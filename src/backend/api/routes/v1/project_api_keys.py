"""Per-user credentials bound immutably to one project."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.db.threads import run_in_session
from core.infra.responses import created_response, success_response
from core.projects.api import require_project_key_access
from core.auth.api_keys import ApiKeyService

router = APIRouter(prefix="/v1/projects", tags=["Project API Keys"])
_ALLOWED_EXPIRY_DAYS = {7, 30, 90, 180, 365}


class CreateProjectApiKeyRequest(BaseModel):
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


class ToggleProjectApiKeyRequest(BaseModel):
    enabled: bool


def _manager(
    project_id: str,
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
    require_project_key_access(db, user.user_id, project_id)
    return user


def _dto(row, plaintext=None):
    return {
        "id": row.id,
        "project_id": row.project_id,
        "name": row.name,
        "key_prefix": row.key_prefix,
        "enabled": row.enabled,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "revealable": bool(row.key_enc),
        "api_key": plaintext,
    }


def _find(db, user, project_id, key_id):
    row = ApiKeyService(db).get_key(user.user_id, key_id, project_id=project_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "api_key_not_found"})
    return row


@router.get("/{project_id}/api-keys", summary="当前账号的项目 API Key")
def list_keys(
    project_id: str, user: UserContext = Depends(_manager), db: Session = Depends(get_db)
):
    rows = ApiKeyService(db).list_keys(user.user_id, project_id=project_id)
    return success_response(data={"items": [_dto(row) for row in rows]})


@router.post("/{project_id}/api-keys", status_code=201, summary="申请项目专属 API Key")
def create_key(
    project_id: str,
    body: CreateProjectApiKeyRequest,
    response: Response,
    user: UserContext = Depends(_manager),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    row, raw = ApiKeyService(db).create_key(
        user.user_id,
        body.name,
        body.expires_in_days,
        project_id=project_id,
    )
    return created_response(data=_dto(row, raw))


@router.get("/{project_id}/api-keys/{key_id}/reveal", summary="复制项目 API Key")
def reveal_key(
    project_id: str,
    key_id: str,
    response: Response,
    user: UserContext = Depends(_manager),
    db: Session = Depends(get_db),
):
    from core.infra.crypto import decrypt_secret

    response.headers["Cache-Control"] = "no-store"
    row = _find(db, user, project_id, key_id)
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


@router.patch("/{project_id}/api-keys/{key_id}", summary="启停项目 API Key")
async def toggle_key(
    project_id: str,
    key_id: str,
    body: ToggleProjectApiKeyRequest,
    user: UserContext = Depends(_manager),
):
    def toggle(db):
        _find(db, user, project_id, key_id)
        return _dto(ApiKeyService(db).set_enabled(user.user_id, key_id, body.enabled, project_id=project_id))

    data = await run_in_session(toggle)
    if not body.enabled:
        from core.auth.api_key_runs import stop_cancelled_key_workers

        await stop_cancelled_key_workers(key_id, user.user_id)
    return success_response(data=data)


@router.delete("/{project_id}/api-keys/{key_id}", summary="撤销项目 API Key")
async def revoke_key(
    project_id: str,
    key_id: str,
    user: UserContext = Depends(_manager),
):
    def revoke(db):
        _find(db, user, project_id, key_id)
        ApiKeyService(db).revoke_key(user.user_id, key_id, project_id=project_id)

    await run_in_session(revoke)
    from core.auth.api_key_runs import stop_cancelled_key_workers

    await stop_cancelled_key_workers(key_id, user.user_id)
    return success_response(data={"id": key_id, "revoked": True})
