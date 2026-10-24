"""Authenticated workspace package assets. Every fetch rechecks the exact binding."""

from features.workbench.api.plugin_ui import _MODULE_CSP
from features.workbench import catalog
from features.workbench.plugin_revisions import resolve_revision
from features.workbench.workspace_page import render
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.capabilities.plugins.resources.installation import safe_path
from features.sites.service import guess_site_mime
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

router = APIRouter(tags=["workbench workspace"])


@router.get("/applications/{key}/packages/{revision}/web/{asset_path:path}")
def asset(
    key: str,
    asset_path: str,
    revision: str,
    instance_id: str | None = Query(None, alias="instanceId", pattern="^[A-Za-z0-9_-]{16,64}$"),
    user: UserContext = Depends(require_auth()),
    db: Session = Depends(get_db),
):
    item = catalog.readable(db, user.user_id, key)
    if item["declaration"]["target"]["kind"] != "plugin":
        raise HTTPException(404, detail={"code": "app_not_found"})
    if not item["availability"]["available"]:
        raise HTTPException(409, detail={"code": item["availability"]["reason"]})
    installation = resolve_revision(db, user.user_id, key, revision, instance_id)
    target = safe_path(installation.package / "web", asset_path)
    if not target.is_file():
        raise HTTPException(404, detail={"code": "package_asset_missing"})
    headers = {
        "Content-Security-Policy": _MODULE_CSP,
        "X-Content-Type-Options": "nosniff",
        "Access-Control-Allow-Origin": "null",
        "Cache-Control": "private, no-store",
    }
    if asset_path.endswith(".html"):
        return HTMLResponse(render(installation.package / "web", asset_path), headers=headers)
    return FileResponse(target, media_type=guess_site_mime(target.name), headers=headers)
