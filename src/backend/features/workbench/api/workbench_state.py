"""Authenticated state boundary, mounted under /v1/workbench."""

from typing import Any, Annotated
from features.workbench import state
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session


class StateScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace: str = Field(
        default="main", min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )
    packageRevision: str = Field(min_length=1, max_length=200)
    installId: str | None = Field(default=None, min_length=1, max_length=512)
    instanceId: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    origin: str | None = Field(default=None, min_length=1, max_length=160)
    executionScope: str | None = Field(default=None, pattern=r"^(local|cloud|host)$")
    schemaVersion: str | None = Field(default=None, min_length=1, max_length=64)


class StateWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    schemaVersion: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]
    reset: bool = Field(default=False, strict=True)


def routes(route_class, json_write):
    router = APIRouter(route_class=route_class)

    @router.get("/me/application-state/{key}")
    def read(
        key: str,
        response: Response,
        scope: Annotated[StateScope, Query()],
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return success_response(
            data=state.read(
                db,
                user.user_id,
                key,
                scope.packageRevision,
                scope.installId,
                scope.workspace,
                scope.schemaVersion,
                scope.origin,
                scope.executionScope,
            )
        )

    @router.put("/me/application-state/{key}", dependencies=[Depends(json_write)])
    def write(
        key: str,
        body: StateWrite,
        response: Response,
        scope: Annotated[StateScope, Query()],
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return success_response(
            data=state.save(
                db,
                user.user_id,
                key,
                scope.packageRevision,
                scope.installId,
                scope.workspace,
                body.model_dump(),
                scope.origin,
                scope.executionScope,
            )
        )

    return router
