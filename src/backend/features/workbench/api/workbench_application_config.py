"""Protected administration of exact installed Workbench configuration."""
from api.deps import require_system_settings, require_admin
from features.workbench.api.workbench_capabilities import CapabilityRoute
from features.workbench import application_config as config
from features.workbench.application_config_bindings import selections, resolve_admin
from features.workbench.external import fail
from core.db.engine import get_db
from core.auth.backend import UserContext, require_auth
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Query, Request, Response, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

router = APIRouter(route_class=CapabilityRoute, prefix="/admin/applications",
                   tags=["workbench installed configuration"])


async def configuration_admin(request: Request,
                              user: UserContext | None = Depends(require_auth(False))):
    if user is not None and user.api_key_id:
        fail(403, "configuration_session_required")
    try:
        return await require_system_settings(request, user)
    except HTTPException:
        await require_admin(request, request.headers.get("authorization"))
        return user.user_id if user is not None else "admin_token"


class Write(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=128)
    values: dict[str, str] = Field(default_factory=dict, max_length=32)
    clearKeys: list[str] = Field(default_factory=list, max_length=32)


@router.get("/configurable")
def configurable(response: Response, offset: int = Query(default=0, ge=0, le=100000),
                 limit: int = Query(default=100, ge=1, le=500),
                 actor: str = Depends(configuration_admin),
                 db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    items = []
    seen = set()
    for item, selected in selections(db, actor):
        if item["applicationKey"] in seen:
            continue
        seen.add(item["applicationKey"])
        try:
            items.append(config.view(db, item, selected))
        except Exception as error:
            from fastapi import HTTPException
            if isinstance(error, HTTPException) and error.status_code in (404, 409):
                continue
            raise
    items.sort(key=lambda item: item["applicationKey"])
    return success_response(data={"items": items[offset:offset + limit],
                                  "offset": offset, "limit": limit,
                                  "hasMore": len(items) > offset + limit})


@router.get("/{key}/config")
def get_configuration(key: str, revision: str, response: Response,
                      actor: str = Depends(configuration_admin),
                      db: Session = Depends(get_db)):
    item, selected = resolve_admin(db, actor, key, revision)
    response.headers["Cache-Control"] = "no-store"
    return success_response(data=config.view(db, item, selected, with_values=True))


@router.put("/{key}/config")
def put_configuration(key: str, body: Write, request: Request, response: Response,
                      actor: str = Depends(configuration_admin),
                      db: Session = Depends(get_db)):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        fail(415, "json_required")
    response.headers["Cache-Control"] = "no-store"
    return success_response(data=config.write(db, actor, key, body.revision,
                                             body.values, body.clearKeys))
