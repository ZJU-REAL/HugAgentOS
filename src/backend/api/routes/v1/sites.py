"""Site management API — a user's own list of sites / detail / edit / delete.

Sites are created by the sites plugin's ``publish_site`` MCP tool (mcp_servers/site_publish_mcp
→ callback into api/routes/v1/internal_sites.py); this route only handles the management surface. Public
hosting lives in ``api/routes/sites_serve.py`` (GET /site/{slug}/…).
"""

from typing import Optional

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.responses import paginated_response, success_response
from core.services.site_access_policy import (
    SiteUpdateScopeFields,
    serialize_site_scope,
    site_management_permission,
    site_scope_ref,
)
from core.services.site_password import MAX_PASSWORD_LENGTH
from core.services.site_service import SiteService
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/sites", tags=["Sites"])


class UpdateSiteRequest(SiteUpdateScopeFields):
    title: Optional[str] = Field(None, description="站点标题", max_length=200)
    slug: Optional[str] = Field(None, description="访问地址 slug", max_length=64)
    description: Optional[str] = Field(None, description="站点描述", max_length=2000)


class RollbackRequest(BaseModel):
    version: int = Field(..., description="要回滚到的历史版本号", ge=1)


class SitePasswordRequest(BaseModel):
    password: str = Field(
        ...,
        description="站点访问密码（明文只用于设置，不会回传）",
        max_length=MAX_PASSWORD_LENGTH,
    )


def _site_to_dict(site, permission: str = "admin") -> dict:
    return {
        "site_id": site.site_id,
        "slug": site.slug,
        "url": f"/site/{site.slug}/",
        "title": site.title,
        "description": site.description,
        **serialize_site_scope(site),
        "entry_file": site.entry_file,
        "current_version": site.current_version,
        "file_count": site.file_count,
        "total_size_bytes": site.total_size_bytes,
        "view_count": site.view_count or 0,
        # 只回传「有没有设密码」，密码本身不出库
        "has_password": bool(site.access_password_hash),
        "chat_id": site.chat_id,
        # project_id set → the site has a source project and can keep being edited via the card's "Edit"; empty for old sites → not editable
        "project_id": getattr(site, "project_id", None),
        "editable": bool(getattr(site, "project_id", None)) and permission in ("edit", "admin"),
        "permission": permission,
        "can_manage": permission == "admin",
        "created_at": site.created_at.isoformat() if site.created_at else None,
        "updated_at": site.updated_at.isoformat() if site.updated_at else None,
    }


@router.get("", summary="获取我的站点列表")
def list_sites(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = SiteService(db)
    items, total = service.list_sites(user.user_id, page, page_size)
    return paginated_response(
        items=[_site_to_dict(s, site_management_permission(db, s, user.user_id)) for s in items],
        page=page,
        page_size=page_size,
        total_items=total,
    )


@router.get("/{site_id}", summary="获取站点详情")
def get_site(
    site_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = SiteService(db)
    site = service.get_owned(site_id, user.user_id, required="view")
    data = _site_to_dict(site, site_management_permission(db, site, user.user_id))
    data["versions"] = (site.extra_data or {}).get("versions") or []
    return success_response(data=data)


@router.patch("/{site_id}", summary="修改站点（标题/可见性/slug/描述）")
def update_site(
    site_id: str,
    body: UpdateSiteRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = SiteService(db)
    site = service.update_site(
        site_id,
        user.user_id,
        title=body.title,
        visibility=body.visibility,
        slug=body.slug,
        description=body.description,
        scope_id=site_scope_ref(body),
    )
    return success_response(data=_site_to_dict(site))


@router.post("/{site_id}/rollback", summary="回滚到历史版本")
def rollback_site(
    site_id: str,
    body: RollbackRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    site = SiteService(db).rollback(site_id, user.user_id, body.version)
    return success_response(data=_site_to_dict(site))


@router.put("/{site_id}/password", summary="设置 / 修改站点访问密码")
def set_site_password(
    site_id: str,
    body: SitePasswordRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    site = SiteService(db).set_access_password(site_id, user.user_id, body.password)
    return success_response(data=_site_to_dict(site))


@router.delete("/{site_id}/password", summary="清除站点访问密码")
def clear_site_password(
    site_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    site = SiteService(db).clear_access_password(site_id, user.user_id)
    return success_response(data=_site_to_dict(site))


@router.delete("/{site_id}", summary="删除站点")
def delete_site(
    site_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = SiteService(db)
    service.delete_site(site_id, user.user_id)
    return success_response(data={"deleted": True})
