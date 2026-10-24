"""API route template.

Replace ${Feature}, ${feature} with actual names.
Feature module: features/${feature}/api/${feature}s.py, registered in api/routes/v1/__init__.py::CE_ROUTERS as
("features.${feature}.api.${feature}s", "router") — or in edition_ee/routes/registry.py::EE_ROUTERS with a policy key,
or via register_router() from features/${feature}/extension.py when the module can be switched off.

Event-loop rules (enforced by tests/test_no_blocking_async_routes.py, tests/test_no_event_loop_in_sync_routes.py,
tests/infra/test_no_session_on_event_loop.py):
  1. Endpoint uses a Session and awaits nothing → declare it `def` (FastAPI runs it in the thread pool).
  2. Endpoint must await network I/O → `async def` WITHOUT `db: Session = Depends(get_db)`; put each DB step in a
     sync helper and call it with `await run_in_session(helper, ...)`. Never hold a Session across an await.
  3. A `def` endpoint has no event loop: no asyncio.create_task / get_running_loop. Durable work → persist and let a
     worker pick it up (orchestration/workers/_worker_base.py); loop-bound fire-and-forget → core.infra.background.spawn.

Module must import on its own without composing the app (tests/test_route_import_order.py).
"""

from typing import Optional

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.threads import run_in_session
from core.infra.responses import created_response, paginated_response, success_response
from fastapi import APIRouter, Depends, Query, status
from features.${feature}.service import ${Feature}Service
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/${feature}s", tags=["${Feature}"])


class Create${Feature}Request(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="名称")
    description: Optional[str] = Field(None, max_length=2000, description="说明")

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class Update${Feature}Request(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    is_enabled: Optional[bool] = None


def _to_dict(item) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "is_enabled": item.is_enabled,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


# --- Pure DB endpoints: plain `def` -----------------------------------------------------------


@router.get("", summary="列出我的${Feature}")
def list_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = ${Feature}Service(db).list_items(str(user.user_id), page, page_size)
    return paginated_response(
        items=[_to_dict(i) for i in items], page=page, page_size=page_size, total_items=total
    )


@router.get("/{item_id}", summary="获取${Feature}详情")
def get_item(item_id: str, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    return success_response(data=_to_dict(${Feature}Service(db).get_owned(item_id, str(user.user_id))))


@router.post("", status_code=status.HTTP_201_CREATED, summary="新建${Feature}")
def create_item(
    body: Create${Feature}Request,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = ${Feature}Service(db).create(str(user.user_id), body.name, body.description)
    return created_response(data=_to_dict(item))


@router.patch("/{item_id}", summary="修改${Feature}")
def update_item(
    item_id: str,
    body: Update${Feature}Request,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = ${Feature}Service(db).update(item_id, str(user.user_id), body.model_dump(exclude_unset=True))
    return success_response(data=_to_dict(item))


@router.delete("/{item_id}", summary="删除${Feature}")
def delete_item(item_id: str, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    ${Feature}Service(db).delete(item_id, str(user.user_id))
    return success_response(data={"id": item_id})


# --- Endpoint that awaits network I/O: async, DB steps off the loop ---------------------------


def _load_for_sync(db: Session, item_id: str, user_id: str) -> dict:
    return _to_dict(${Feature}Service(db).get_owned(item_id, user_id))


def _record_sync_result(db: Session, item_id: str, user_id: str, remote: dict) -> dict:
    item = ${Feature}Service(db).update(item_id, user_id, {"description": remote.get("summary")})
    return _to_dict(item)


@router.post("/{item_id}/sync", summary="与外部系统同步")
async def sync_item(item_id: str, user: UserContext = Depends(get_current_user)):
    user_id = str(user.user_id)
    current = await run_in_session(_load_for_sync, item_id, user_id)   # DB step 1, session closed after
    remote = {"summary": current["name"]}                              # await some_client.fetch(...) here
    data = await run_in_session(_record_sync_result, item_id, user_id, remote)  # DB step 2
    return success_response(data=data)
