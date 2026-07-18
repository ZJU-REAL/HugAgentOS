"""${FEATURE_NAME} API routes (v1).

Replace ${FEATURE_NAME}, ${feature}, ${Feature} with actual names.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.deps import require_admin
from core.auth.backend import get_current_user, UserContext
from core.db.engine import get_db
from core.infra.exceptions import ResourceNotFoundError, BadRequestError
from core.infra.logging import get_logger
from core.infra.responses import success_response, created_response, paginated_response
from core.services.${feature}_service import ${Feature}Service

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/${feature}s", tags=["${Feature}s"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class Create${Feature}Request(BaseModel):
    """创建 ${Feature} 请求体."""
    name: str = Field(..., description="名称", min_length=1, max_length=200)
    description: Optional[str] = Field(None, description="描述")
    metadata: Optional[dict] = Field(default_factory=dict, description="扩展信息")


class Update${Feature}Request(BaseModel):
    """更新 ${Feature} 请求体."""
    name: Optional[str] = Field(None, description="名称", max_length=200)
    description: Optional[str] = Field(None, description="描述")
    metadata: Optional[dict] = Field(None, description="扩展信息")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_dict(item) -> dict:
    """ORM → API response dict."""
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "metadata": item.extra_data or {},
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", summary="获取${Feature}列表")
async def list_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ${Feature}Service(db)
    items, total, total_pages = service.list_items(user.user_id, page, page_size)
    return paginated_response(
        items=[_to_dict(i) for i in items],
        page=page,
        page_size=page_size,
        total_items=total,
    )


@router.get("/{item_id}", summary="获取${Feature}详情")
async def get_item(
    item_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ${Feature}Service(db)
    item = service.get_item(item_id, user.user_id)
    if not item:
        raise ResourceNotFoundError("${feature}", item_id)
    return success_response(data=_to_dict(item))


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建${Feature}")
async def create_item(
    body: Create${Feature}Request,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ${Feature}Service(db)
    item = service.create(
        user_id=user.user_id,
        name=body.name,
        description=body.description,
        metadata=body.metadata,
    )
    return created_response(data=_to_dict(item))


@router.put("/{item_id}", summary="更新${Feature}")
async def update_item(
    item_id: str,
    body: Update${Feature}Request,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ${Feature}Service(db)
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise BadRequestError("No fields to update")
    item = service.update(item_id, user.user_id, update_data)
    if not item:
        raise ResourceNotFoundError("${feature}", item_id)
    return success_response(data=_to_dict(item))


@router.delete("/{item_id}", summary="删除${Feature}")
async def delete_item(
    item_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ${Feature}Service(db)
    ok = service.delete(item_id, user.user_id)
    if not ok:
        raise ResourceNotFoundError("${feature}", item_id)
    return success_response(message="Deleted")
