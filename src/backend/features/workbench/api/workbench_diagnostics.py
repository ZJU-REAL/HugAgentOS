"""Administrator diagnostic projection plus owner-only user failure receipts."""

from typing import Any, Literal

from api.deps import require_config
from features.workbench import catalog, diagnostics, external, plugin_catalog, origin
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.db.models import ContentBlock, UserShadow
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session


def summary(db, item, available=None, checked=False):
    binding = item.get("binding") or {}
    auth_record = db.get(ContentBlock, "workbench_external_auth:" + item["applicationKey"])
    auth = auth_record.payload if auth_record else {}
    return {
        "applicationKey": item["applicationKey"],
        "source": item["applicationKey"].split(":")[0],
        "revision": diagnostics.revision(item["revision"]),
        "enabled": item["enabled"],
        "installId": diagnostics.opaque(binding.get("installId")),
        "scope": binding.get("executionScope", "cloud"),
        "authentication": (
            "host_session"
            if item["declaration"]["target"]["kind"] != "external"
            else (
                auth.get("mode")
                if auth.get("mode") in {"shared_identity", "exchange"}
                else "independent"
            )
        ),
        "availability": available
        or {"available": item["enabled"], "reason": item.get("unavailableReason")},
        "permissionStatus": (
            "authorized"
            if available and available["available"]
            else (
                "denied"
                if checked and available is None
                else "unavailable" if checked else "requires_user_context"
            )
        ),
    }


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    declaration: dict[str, Any]


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Literal["bridge_timeout", "page_failed"]


def routes(route_class, require_json_write):
    router = APIRouter(route_class=route_class)
    admin = [Depends(require_config)]
    write = admin + [Depends(require_json_write)]

    @router.get("/admin/diagnostics", dependencies=admin)
    def administrative(
        response: Response, user_id: str | None = None, db: Session = Depends(get_db)
    ):
        response.headers["Cache-Control"] = "no-store"
        items = catalog.definitions(db) + plugin_catalog.administrative_items(db)
        from core.db.models import InstalledPlugin

        source = origin.current(db)
        installations = {
            plugin_catalog.application_key(row, app["id"], source): row
            for row in db.query(InstalledPlugin).all()
            for app in (row.ui_contributions or {}).get("contributes", {}).get("applications", [])
            if isinstance(app, dict) and isinstance(app.get("id"), str)
        }
        for item in items:
            row = installations.get(item["applicationKey"])
            if row is not None:
                item["revision"] = (row.import_report or {}).get("package_revision") or row.version
                item["binding"] = {"installId": row.install_id, "executionScope": source["kind"],
                                   "origin": source}
        visible = {}
        if user_id is not None:
            if len(user_id) > 200 or db.get(UserShadow, user_id) is None:
                raise HTTPException(404, detail={"code": "user_not_found"})
            visible = {
                i["applicationKey"]: i["availability"]
                for i in catalog.directory(db, user_id)["items"]
            }
        recent = diagnostics.recent(db)
        latest = {}
        for event in recent:
            if event["code"] != "ok" and event["appKey"] not in latest:
                latest[event["appKey"]] = {
                    "diagnosticId": event["diagnosticId"],
                    "code": event["code"],
                    "timestamp": event["timestamp"],
                }
        summaries = [
            summary(db, i, visible.get(i["applicationKey"]), user_id is not None) for i in items
        ]
        for value in summaries:
            value["recentFailure"] = latest.get(value["applicationKey"])
        return success_response(
            data={
                "applications": summaries,
                "recent": recent,
                "metrics": diagnostics.metrics(db),
            }
        )

    @router.get("/me/diagnostics/resource-metrics")
    def own_resources(
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        from features.workbench.diagnostic_resources import snapshot

        response.headers["Cache-Control"] = "private, no-store"
        return success_response(data=snapshot(db, user.user_id))

    @router.get("/me/diagnostics/{identity}")
    def receipt(
        identity: str,
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return success_response(data=diagnostics.own(db, user.user_id, identity))

    @router.get("/me/diagnostics")
    def mine(
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return success_response(data={"items": diagnostics.recent(db, user.user_id)})

    @router.post("/applications/{key}/diagnostics", dependencies=[Depends(require_json_write)])
    def failure(
        key: str,
        body: Failure,
        response: Response,
        user: UserContext = Depends(require_auth()),
        db: Session = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "no-store"
        item = catalog.readable(db, user.user_id, key)
        value = diagnostics.record(db, user.user_id, "handshake", body.code, item)
        return success_response(data=value)

    @router.post("/admin/diagnostics/preview", dependencies=write)
    def preview(body: Preview, response: Response, db: Session = Depends(get_db)):
        response.headers["Cache-Control"] = "no-store"
        external.validate_definition(db, body.declaration)
        return success_response(
            data={"valid": True, "source": "external", "authentication": "independent"}
        )

    @router.post("/admin/diagnostics/probe/{key}", dependencies=write)
    def check(key: str, response: Response, db: Session = Depends(get_db)):
        from features.workbench.diagnostic_probe import probe

        response.headers["Cache-Control"] = "no-store"
        item = external.record(db, key)
        external.validate_definition(db, item["declaration"])
        try:
            result = probe(item["declaration"]["target"]["url"])
            diagnostics.record(db, "config", "probe", "ok", item)
            return success_response(data=result)
        except HTTPException as error:
            value = diagnostics.record(db, "config", "probe", "probe_failed", item)
            outcome = (
                error.detail.get("code", "probe_failed")
                if isinstance(error.detail, dict)
                else "probe_failed"
            )
            raise HTTPException(
                error.status_code,
                detail={"code": outcome, "diagnosticId": value["diagnosticId"]},
                headers={"Cache-Control": "no-store"},
            ) from None

    return router
