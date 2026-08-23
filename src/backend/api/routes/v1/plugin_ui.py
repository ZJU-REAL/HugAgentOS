"""Plugin UI contribution endpoints — the host side of the L0/L1/L2 contract.

GET  /v1/plugins/ui-contributions              每个已安装且启用的插件贡献的界面声明
POST /v1/plugins/{slug}/data/{source_id}       L1：受控调用插件声明的上游数据源
GET  /v1/plugins/{slug}/web/{path}             L2：托管插件包内自带的前端模块资产

These three replace what used to be per-plugin routes bolted onto the host: a
plugin that needed the browser to reach its upstream, or to render a view the
host did not have, used to get its own hard-coded endpoint.  Nothing here knows
any plugin by name — the manifest supplies the specifics.

Authorization is uniform: every endpoint resolves the caller's *installed and
enabled* plugins first, so knowing a slug is never sufficient — the plugin has
to actually be available to that user.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.exceptions import ResourceNotFoundError
from core.infra.responses import success_response
from core.services import plugin_data_proxy as proxy
from core.services import plugin_service as ps
from core.services.plugin_ui_contract import find_data_source, find_module

router = APIRouter(prefix="/v1/plugins", tags=["Plugin UI"])
logger = logging.getLogger(__name__)

# Static assets shipped inside a plugin package run in a null-origin iframe and
# must never be able to reach back out on their own: everything they need comes
# through the postMessage bridge.
_MODULE_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors *"
)



@router.get("/ui-contributions", summary="已安装插件贡献的界面声明")
async def list_ui_contributions(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """UI declarations of every plugin the caller has installed and enabled.

    The frontend renders entirely from this payload, which is why disabling a
    plugin removes its interface without any host-side branch.  Data-source
    definitions are projected down to id + parameter schema; the upstream URL
    and auth header stay on the server.
    """
    return success_response(data={"items": ps.resolve_ui_contributions(db, str(user.user_id))})


def _require_ui(db: Session, slug: str, user_id: str) -> Dict[str, Any]:
    ui = ps.resolve_installed_ui(db, slug, user_id)
    if ui is None:
        raise ResourceNotFoundError("plugin_ui", slug)
    return ui


@router.post("/{slug}/data/{source_id}", summary="调用插件声明的数据源")
async def call_plugin_data_source(
    request: Request,
    slug: str = Path(..., min_length=1, max_length=100),
    source_id: str = Path(..., min_length=1, max_length=64),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Run one manifest-declared upstream call on the caller's behalf.

    Params are validated against the declaration's ``params_schema`` and
    credentials are interpolated server-side, so the browser never sees them.
    """
    ui = _require_ui(db, slug, str(user.user_id))
    source = find_data_source(ui, source_id)
    if source is None:
        raise ResourceNotFoundError("plugin_data_source", f"{slug}/{source_id}")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    params = payload if isinstance(payload, dict) else {}
    data = await proxy.call_data_source(source, params, slug=slug)
    return success_response(data=data)


@router.get("/{slug}/web/{asset_path:path}", summary="插件自带前端模块的静态资源")
async def get_plugin_web_asset(
    slug: str = Path(..., min_length=1, max_length=100),
    asset_path: str = "",
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Serve one file from the plugin package's own ``web/`` directory.

    Plugin UI code lives with the plugin, never in the host frontend tree, so
    this is the only path by which it reaches the browser.  Responses carry a
    restrictive CSP (notably ``connect-src 'none'``) and are sandboxed by the
    embedding iframe.
    """
    from core.services.site_service import guess_site_mime

    _require_ui(db, slug, str(user.user_id))
    target = proxy.module_asset_path(slug, asset_path)
    if target is None:
        raise ResourceNotFoundError("plugin_web_asset", f"{slug}/{asset_path}")
    media_type = guess_site_mime(target.name)
    return FileResponse(
        target,
        media_type=media_type,
        headers={
            "Content-Security-Policy": _MODULE_CSP,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        },
    )


__all__ = ["router"]
