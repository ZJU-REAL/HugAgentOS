"""Private owner and administrator upgrade decisions; candidate data never grants access."""

from api.deps import require_admin
from features.workbench import plugin_upgrades
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from typing import Literal


class UpgradeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectedRevision: str = Field(min_length=1, max_length=128)
    candidateRevision: str = Field(pattern="^(?:[a-f0-9]{12}|[a-f0-9]{64})$")
    decision: Literal["approve", "reject"]
    resetState: bool = Field(default=False, strict=True)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=32)


def routes(route_class, require_json_write):
    router = APIRouter(route_class=route_class)

    @router.get("/plugin-upgrades/{install_id}")
    def read(
        install_id: str,
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "no-store"
        return success_response(data=plugin_upgrades.read(db, install_id, user.user_id))

    @router.post(
        "/plugin-upgrades/{install_id}/decision", dependencies=[Depends(require_json_write)]
    )
    def decide(
        install_id: str,
        body: UpgradeDecision,
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        from api.routes.v1.plugins import _require_can_import_plugin

        _require_can_import_plugin(user.user_id, db)
        response.headers["Cache-Control"] = "no-store"
        return success_response(
            data=plugin_upgrades.decide(
                db,
                install_id,
                user.user_id,
                body.expectedRevision,
                body.candidateRevision,
                body.decision,
                body.resetState,
                body.secrets,
            )
        )

    @router.get("/admin/plugin-upgrades/{install_id}", dependencies=[Depends(require_admin)])
    def admin_read(install_id: str, response: Response, db: Session = Depends(get_db)):
        response.headers["Cache-Control"] = "no-store"
        return success_response(data=plugin_upgrades.read(db, install_id, None))

    @router.post(
        "/admin/plugin-upgrades/{install_id}/decision",
        dependencies=[Depends(require_admin), Depends(require_json_write)],
    )
    def admin_decide(
        install_id: str, body: UpgradeDecision, response: Response, db: Session = Depends(get_db)
    ):
        response.headers["Cache-Control"] = "no-store"
        return success_response(
            data=plugin_upgrades.decide(
                db,
                install_id,
                None,
                body.expectedRevision,
                body.candidateRevision,
                body.decision,
                body.resetState,
                body.secrets,
            )
        )

    return router
