"""Authenticated activity, consent and background stop. No worker-launch endpoint."""

from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from features.workbench import activity, background
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.db.threads import run_in_session
from core.infra.responses import success_response


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Register(Body):
    binding: dict[str, Any]
    moduleId: str | None = Field(default=None, max_length=64)


class Consent(Body):
    capabilities: list[str] = Field(max_length=3)


class Badge(Body):
    moduleId: str | None = Field(default=None, max_length=64)
    count: int = Field(strict=True, ge=0, le=9999)


class Notification(Body):
    moduleId: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=240)


class Background(Body):
    resourceId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


def private(response: Response):
    response.headers["Cache-Control"] = "private, no-store"


def _check_declared(db, user_id, key, instance_id, capabilities):
    _, row, item = activity.authorize(db, user_id, key, instance_id)
    if set(capabilities) - activity.declared(item, db, user_id, row.get("moduleId")):
        from features.workbench.external import fail

        fail(403, "capability_not_declared")


def routes(route_class, json_write):
    router = APIRouter(
        route_class=route_class, dependencies=[Depends(require_auth()), Depends(private)]
    )
    path = "/applications/{key}/instances/{instance_id}/activity"

    @router.get("/me/activity")
    async def projection(
        response: Response,
        user: UserContext = Depends(require_auth()),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        await background.reconcile(user.user_id)
        return success_response(data=await run_in_session(activity.projection, user.user_id))

    @router.post(path, dependencies=[Depends(json_write)])
    def register(
        key: str,
        instance_id: str,
        body: Register,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        return success_response(
            data=activity.register(db, user.user_id, key, instance_id, body.binding, body.moduleId)
        )

    @router.get(path + "/consent")
    def grants(
        key: str,
        instance_id: str,
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        value, row, _ = activity.authorize(db, user.user_id, key, instance_id)
        grant = value["grants"].get(key, {})
        return success_response(
            data={
                "capabilities": (
                    grant.get("capabilities", []) if grant.get("binding") == row["binding"] else []
                )
            }
        )

    @router.post(path + "/consent", dependencies=[Depends(json_write)])
    async def consent(
        key: str,
        instance_id: str,
        body: Consent,
        user: UserContext = Depends(require_auth()),
    ):
        await run_in_session(_check_declared, user.user_id, key, instance_id, body.capabilities)
        if "background" not in body.capabilities:
            await background.stop_all(user.user_id, application_key=key)
        return success_response(
            data=await run_in_session(
                activity.consent, user.user_id, key, instance_id, body.capabilities
            )
        )

    @router.put(path + "/badge", dependencies=[Depends(json_write)])
    def badge(
        key: str,
        instance_id: str,
        body: Badge,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        return success_response(
            data=activity.badge(db, user.user_id, key, instance_id, body.count, body.moduleId)
        )

    @router.post(path + "/notification", dependencies=[Depends(json_write)])
    def notify(
        key: str,
        instance_id: str,
        body: Notification,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        return success_response(
            data=activity.notify(
                db, user.user_id, key, instance_id, body.title, body.body, body.moduleId
            )
        )

    @router.delete(path)
    def close(
        key: str,
        instance_id: str,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        activity.clear_instance(db, user.user_id, key, instance_id)
        return success_response(data={})

    @router.delete("/me/activity/notifications/{notification_id}")
    def dismiss(
        notification_id: str,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        activity.dismiss(db, user.user_id, notification_id)
        return success_response(data={})

    @router.post(path + "/background", dependencies=[Depends(json_write)])
    async def start(
        key: str,
        instance_id: str,
        body: Background,
        user: UserContext = Depends(require_auth()),
    ):
        return success_response(
            data=await background.start(user.user_id, key, instance_id, body.resourceId)
        )

    @router.post(path + "/background/{resource_id}/lease", dependencies=[Depends(json_write)])
    async def renew(
        key: str,
        instance_id: str,
        resource_id: str,
        user: UserContext = Depends(require_auth()),
    ):
        return success_response(
            data=await background.renew(user.user_id, key, instance_id, resource_id)
        )

    @router.delete(path + "/background/{resource_id}")
    async def stop(
        key: str,
        instance_id: str,
        resource_id: str,
        user: UserContext = Depends(require_auth()),
    ):
        await background.stop(user.user_id, key, instance_id, resource_id)
        return success_response(data={})

    return router
