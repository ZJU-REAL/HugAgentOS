"""Workbench directory and ordinary external links; managed SQL/MCP apps stay separate."""

from features.workbench.coordination import configuration


from typing import Any

from api.deps import console_actor, require_config
from features.workbench.api.workbench_auth import browser_write
from features.workbench.api.workbench_auth import router as auth_router
from features.workbench.diagnostics import observed
from features.workbench import catalog, external, external_auth, policy, origin
from features.workbench.directory_revision import matches, snapshot
from features.workbench.external_embed import EmbeddingPolicy, launch_policy, validate_policy
from features.workbench.external_boundary import validate_cookie_scope
from core.auth.backend import UserContext, require_auth
from core.config.settings import settings
from core.db.engine import get_db
from core.db.models import ContentBlock
from core.infra.logging import trace_id_var
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session


class WorkbenchRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def sanitized(request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                raise HTTPException(
                    422,
                    detail={
                        "code": "invalid_request",
                        "fields": [
                            {"path": list(e["loc"]), "code": e["type"]} for e in error.errors()
                        ],
                    },
                    headers={"Cache-Control": "no-store"},
                ) from None
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                raise

        return sanitized


def require_origin(request: Request, db: Session = Depends(get_db)):
    origin.require_current(request, db)


router = APIRouter(
    prefix="/v1/workbench",
    tags=["workbench"],
    route_class=WorkbenchRoute,
    dependencies=[Depends(require_origin)],
)


def require_json_write(request: Request):
    # JSON requires a CORS preflight for cross-origin browser writes. Reject
    # simple form/blob submissions, including requests with no Content-Type.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, detail={"code": "json_required"})


class ExternalWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    declaration: dict[str, Any]
    enabled: bool = Field(strict=True)
    embedding: EmbeddingPolicy | None = None


class ExternalUpdate(ExternalWrite):
    revision: int = Field(ge=1, strict=True)


@router.get("/admin/external-applications", dependencies=[Depends(require_config)])
def admin_list(response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return success_response(
        data={"items": [item for item in external.items(db) if not item.get("legacy")]}
    )


@router.get("/admin/audit", dependencies=[Depends(require_config)])
def admin_audit(response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    rows = (
        db.query(ContentBlock)
        .filter(ContentBlock.id.startswith(external.AUDIT, autoescape=True))
        .order_by(ContentBlock.updated_at.desc())
        .limit(100)
        .all()
    )
    return success_response(data={"items": [r.payload for r in rows]})


@router.post(
    "/admin/external-applications",
    dependencies=[Depends(require_config), Depends(require_json_write)],
)
def create_external(
    body: ExternalWrite,
    request: Request,
    response: Response,
    actor: str = Depends(console_actor),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    external.validate_definition(db, body.declaration)
    embedding = validate_policy(
        body.declaration["target"],
        body.embedding.model_dump() if body.embedding else None,
        str(request.base_url),
    )
    return success_response(
        data=external.save(
            db, body.declaration, body.enabled, actor, trace_id_var.get(), embedding=embedding
        )
    )


@router.put(
    "/admin/external-applications/{key}",
    dependencies=[Depends(require_config), Depends(require_json_write)],
)
def update_external(
    key: str,
    body: ExternalUpdate,
    request: Request,
    response: Response,
    actor: str = Depends(console_actor),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    external.validate_definition(db, body.declaration)
    embedding = validate_policy(
        body.declaration["target"],
        body.embedding.model_dump() if body.embedding else None,
        str(request.base_url),
    )
    return success_response(
        data=external.save(
            db,
            body.declaration,
            body.enabled,
            actor,
            trace_id_var.get(),
            body.revision,
            key,
            embedding,
        )
    )


@router.get("/applications")
def directory(
    response: Response, user: UserContext = Depends(require_auth()), db: Session = Depends(get_db)
):
    response.headers["Cache-Control"] = "private, no-store"
    return success_response(
        data={
            "items": [
                i
                for i in catalog.directory(db, user.user_id)["items"]
                if i["applicationKey"].startswith("external:") and i["enabled"]
            ]
        }
    )


@router.get("/applications/{key}")
def application(
    key: str,
    response: Response,
    user: UserContext = Depends(require_auth()),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return success_response(data=catalog.readable(db, user.user_id, key, include_disabled=True))


@router.post("/applications/{key}/launch")
@observed("launch")
def launch(
    key: str,
    request: Request,
    response: Response,
    moduleId: str | None = None,
    user: UserContext = Depends(require_auth()),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    if key.startswith(("external:", "legacy:")):
        # Authorization observes the locked current configuration.
        catalog.readable(db, user.user_id, key)
        browser_write(request)
        configuration(db)
        db.expire_all()
    item = catalog.readable(db, user.user_id, key)
    if not item["availability"]["available"]:
        external.fail(409, item["availability"]["reason"])
    if item["declaration"]["target"]["kind"] == "plugin":
        from features.workbench.plugin_catalog import launch as plugin_launch

        return success_response(data=plugin_launch(db, user.user_id, item, moduleId))
    if item["declaration"]["target"]["kind"] == "external":
        validate_cookie_scope(
            request,
            item["declaration"]["target"],
            external_auth.read(db, key),
            settings.session.cookie_domain,
        )
    target = external_auth.launch(
        db,
        user.user_id,
        item,
        (
            external_auth.digest(request.cookies[settings.session.cookie_name])
            if request.cookies.get(settings.session.cookie_name)
            else None
        ),
    )
    # Finish the locked launch transaction before the independent diagnostic write.
    db.commit()
    return success_response(
        data={
            "applicationKey": key,
            "revision": item["revision"],
            "target": target,
            **launch_policy(item, str(request.base_url), target),
        }
    )


class PolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    defaultPinned: list[str] = Field(max_length=100)
    forced: list[str] = Field(max_length=20)
    hidden: list[str] = Field(default_factory=list, max_length=100)


@router.get("/catalog")
@observed("directory")
def catalog_directory(
    request: Request,
    response: Response,
    user: UserContext = Depends(require_auth()),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    data, _ = snapshot(db, user.user_id)
    headers = {
        "Cache-Control": "private, no-cache",
        "ETag": '"' + data["revision"] + '"',
        "Vary": "Cookie, Authorization, X-HugAgentOS-Target",
    }
    if matches(request.headers.get("if-none-match"), data["revision"]):
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=data)


@router.get("/admin/catalog", dependencies=[Depends(require_config)])
def admin_catalog(response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    from features.workbench.plugin_catalog import administrative_items

    return success_response(data={"items": catalog.definitions(db) + administrative_items(db)})


@router.get("/admin/policy", dependencies=[Depends(require_config)])
def admin_policy(response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return success_response(data=policy.read(db))


@router.put("/admin/policy", dependencies=[Depends(require_config), Depends(require_json_write)])
def update_policy(
    body: PolicyWrite,
    response: Response,
    actor: str = Depends(console_actor),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return success_response(data=policy.save(db, body.model_dump(), actor, trace_id_var.get()))


@router.get("/admin/legacy-config", dependencies=[Depends(require_config)])
def admin_legacy_config(response: Response, db: Session = Depends(get_db)):
    from features.workbench import migration_compat

    response.headers["Cache-Control"] = "no-store"
    return success_response(
        data={
            "app_config": migration_compat.app_config(db),
            "app_config_updated_at": migration_compat.version(db),
        }
    )


router.include_router(auth_router)

from features.workbench.api.workbench_preferences import routes as preference_routes

router.include_router(preference_routes(WorkbenchRoute, require_json_write))
from features.workbench.api.workbench_workspace import router as workspace_router

router.include_router(workspace_router)

from features.workbench.api.workbench_resources import routes as resource_routes

router.include_router(resource_routes(WorkbenchRoute, require_json_write))

from features.workbench.api.workbench_state import routes as state_routes

router.include_router(state_routes(WorkbenchRoute, require_json_write))

from features.workbench.api.workbench_capabilities import router as capability_router

router.include_router(capability_router)

from features.workbench.api.workbench_events import routes as event_routes

router.include_router(event_routes(WorkbenchRoute))

from features.workbench.api.workbench_activity import routes as activity_routes
from features.workbench.api.workbench_diagnostics import routes as diagnostic_routes
from features.workbench.api.workbench_upgrades import routes as upgrade_routes
from features.workbench.api.workbench_revision_leases import routes as revision_routes

router.include_router(activity_routes(WorkbenchRoute, require_json_write))
router.include_router(diagnostic_routes(WorkbenchRoute, require_json_write))
router.include_router(upgrade_routes(WorkbenchRoute, require_json_write))
router.include_router(revision_routes(WorkbenchRoute, require_json_write))

from features.workbench.api.workbench_application_config import router as application_config_router

router.include_router(application_config_router)
