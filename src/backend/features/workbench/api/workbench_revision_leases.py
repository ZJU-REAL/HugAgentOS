"""Revision retention is bounded and tied to an authenticated running instance."""

from features.workbench import plugin_revisions
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field


class LeaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern="^(?:[a-f0-9]{12}|[a-f0-9]{64})$")
    instanceId: str = Field(pattern="^[A-Za-z0-9_-]{16,64}$")


def routes(route_class, require_json_write):
    router = APIRouter(route_class=route_class, dependencies=[Depends(require_json_write)])

    @router.post("/applications/{key}/revision-leases")
    def create(
        key: str,
        body: LeaseBody,
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "no-store"
        return success_response(
            data=plugin_revisions.create(db, user.user_id, key, body.revision, body.instanceId)
        )

    @router.put("/applications/{key}/revision-leases")
    def renew(
        key: str,
        body: LeaseBody,
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "no-store"
        return success_response(
            data=plugin_revisions.renew(db, user.user_id, key, body.revision, body.instanceId)
        )

    @router.post("/applications/{key}/revision-leases/close")
    def close(
        key: str,
        body: LeaseBody,
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "no-store"
        return success_response(
            data=plugin_revisions.close(db, user.user_id, key, body.revision, body.instanceId)
        )

    return router
