"""Plugin user-facing API — browse the plugin marketplace + install from it + import Claude Code / Codex plugins.

GET    /v1/plugins                          plugin marketplace list (flags whether already installed)
GET    /v1/plugins/installed                my installed plugins
GET    /v1/plugins/installed/{id}/detail    installed plugin detail (incl. skill/MCP components and their details)
GET    /v1/plugins/{slug}                   marketplace plugin detail (component manifest + required secrets + dropped preview)
POST   /v1/plugins/{slug}/install           install from the plugin marketplace (private, owner = current user)
POST   /v1/plugins/import                   upload a .zip to import an external plugin (native/CC/Codex)
DELETE /v1/plugins/installed/{id}           uninstall
PATCH  /v1/plugins/installed/{id}/enable    overall on/off switch

Permissions: browsing/viewing details is open to all logged-in users; **both installing from the
marketplace and importing a zip require ``can_import_plugin``** (same as the skill marketplace's
``can_add_skill``, granted per user via the Config backend "User Management -> Permission Config",
off by default). Admin global install goes through ``admin_plugins``. Installed/imported components
automatically appear in the "mine" section of ``/v1/catalog`` and are registered to the agent
(owned private items, effective once is_enabled=True).
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError, BadRequestError
from core.infra.responses import created_response, success_response
from core.services import marketplace_listing as ml
from core.services import plugin_service as ps

router = APIRouter(prefix="/v1/plugins", tags=["Plugin"])
logger = logging.getLogger(__name__)


def _parse_secrets(secrets: Optional[str]) -> Dict[str, str]:
    """Parse the secrets JSON string from the multipart form into a dict (400 if invalid)."""
    if not secrets:
        return {}
    try:
        parsed = json.loads(secrets)
    except (TypeError, ValueError):
        raise BadRequestError(message="secrets 必须是合法 JSON 对象")
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def _require_can_import_plugin(user_id: str, db: Session) -> None:
    """Plugin install/import permission: personal explicit (user management) -> team default (team management) -> off by default."""
    if not resolve_user_capabilities(db, user_id)["can_import_plugin"]:
        raise AccessDeniedError(message="管理员未开放插件安装/导入功能", reason="can_import_plugin_disabled")


@router.get("", summary="内置插件包列表")
async def list_plugins(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = ps.list_plugins(db, owner_user_id=str(user.user_id))
    return success_response(data={"items": items})


@router.get("/installed", summary="我已安装的插件")
async def list_installed(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(data={"items": ps.list_installed(
        db, owner_user_id=str(user.user_id), include_global=True
    )})


@router.get("/installed/{install_id}/detail", summary="已安装插件详情（含组件）")
async def get_installed_detail(
    install_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """已安装插件的完整详情：技能（含指令/文件清单）+ MCP（含工具列表）。"""
    return success_response(data=ps.get_installed_detail(
        db, install_id, owner_user_id=str(user.user_id)
    ))


@router.get("/{slug}", summary="内置插件详情")
async def get_plugin(
    slug: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ml.ensure_item_visible(db, ml.KIND_PLUGIN, slug, str(user.user_id), resource="plugin")
    return success_response(data=ps.get_plugin_detail(slug, db))


class InstallRequest(BaseModel):
    secrets: Dict[str, str] = Field(default_factory=dict, description="凭据键值（按 required_secrets 提供）")


@router.post("/{slug}/install", status_code=201, summary="从插件市场安装（私有）")
async def install_plugin(
    slug: str,
    body: InstallRequest = InstallRequest(),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Both installing from and importing into the plugin marketplace require can_import_plugin
    # (same as the skill marketplace's can_add_skill); granted per user via the Config backend
    # "User Management -> Permission Config". Admin global install goes through admin_plugins.
    _require_can_import_plugin(str(user.user_id), db)
    ml.ensure_item_visible(db, ml.KIND_PLUGIN, slug, str(user.user_id), resource="plugin")
    result = ps.install_plugin(
        db, slug, owner_user_id=str(user.user_id),
        secrets=body.secrets, created_by=str(user.user_id),
    )
    return created_response(data=result)


@router.post("/import", status_code=201, summary="导入外部插件（上传 zip）")
async def import_plugin(
    file: UploadFile = File(..., description="插件包 .zip（native / Claude Code / Codex）"),
    secrets: Optional[str] = Form(None, description="凭据 JSON 字符串"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传插件 zip 并导入（私有）。返回 import_report（imported / adapted / dropped）。"""
    _require_can_import_plugin(str(user.user_id), db)
    raw = await file.read()
    secret_map = _parse_secrets(secrets)
    result = ps.import_plugin_from_zip(
        db, raw, owner_user_id=str(user.user_id),
        secrets=secret_map, created_by=str(user.user_id),
    )
    return created_response(data=result)


@router.delete("/installed/{install_id}", summary="卸载插件")
async def uninstall_plugin(
    install_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = ps.uninstall_plugin(db, install_id, owner_user_id=str(user.user_id))
    return success_response(data=result)


class EnableRequest(BaseModel):
    enabled: bool = Field(..., description="开/关")


@router.patch("/installed/{install_id}/enable", summary="整体开关插件")
async def set_enabled(
    install_id: str,
    body: EnableRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Each user toggles on their own (writes a per-user override); global plugins can also be
    # enabled/disabled per user without affecting others.
    result = ps.set_plugin_enabled_for_user(
        db, install_id, enabled=body.enabled, user_id=str(user.user_id)
    )
    return success_response(data=result)
