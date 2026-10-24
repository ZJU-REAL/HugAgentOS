"""Authenticated self navigation preferences; never accepts a user identifier."""

from typing import Literal

from features.workbench import preferences
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

Environment = Literal["web", "cloud", "local", "dual"]


class PreferenceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    pinned: list[str] | None = Field(max_length=200)
    order: list[str] = Field(max_length=200)


def routes(route_class, json_write):
    router = APIRouter(route_class=route_class)

    @router.get("/me/navigation-preferences")
    def read(
        response: Response,
        environment: Environment = "web",
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return success_response(data=preferences.read(db, user.user_id, environment))

    @router.put("/me/navigation-preferences", dependencies=[Depends(json_write)])
    def write(
        body: PreferenceWrite,
        response: Response,
        environment: Environment = "web",
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return success_response(
            data=preferences.save(db, user.user_id, environment, body.model_dump())
        )

    return router
