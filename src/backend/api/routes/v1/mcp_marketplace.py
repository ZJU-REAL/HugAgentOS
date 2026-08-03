"""User-facing MCP marketplace: browse, install, and submit private remote MCPs."""

from __future__ import annotations

from typing import Dict, List, Optional

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError
from core.infra.responses import created_response, success_response
from core.services import mcp_marketplace_service as market
from core.services import mcp_oauth_service
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/mcp-market", tags=["MCP Marketplace"])


def _require_can_add_mcp(user_id: str, db: Session) -> None:
    if not resolve_user_capabilities(db, user_id).get("can_add_mcp", False):
        raise AccessDeniedError(
            message="管理员未开放 MCP 安装/上架功能",
            reason="can_add_mcp_disabled",
        )


@router.get("/items", summary="MCP 市场列表")
async def list_items(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    market.ensure_curated_market_items(db)
    return success_response(
        data=market.list_market_items(
            db,
            owner_user_id=str(user.user_id),
            viewer_user_id=str(user.user_id),
        )
    )


@router.get("/items/{slug}", summary="MCP 市场详情")
async def get_item(
    slug: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(
        data=market.get_market_item(
            db,
            slug,
            owner_user_id=str(user.user_id),
            viewer_user_id=str(user.user_id),
        )
    )


class InstallRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=128)
    auth_method: Optional[str] = Field(None, min_length=1, max_length=64)
    credentials: Dict[str, str] = Field(default_factory=dict)
    confirm_high_risk: bool = False


class OAuthStartRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=128)
    auth_method: str = Field(..., min_length=1, max_length=64)
    credentials: Dict[str, str] = Field(default_factory=dict)
    client_id: str = Field("", max_length=512)
    client_secret: str = Field("", max_length=2000)
    confirm_high_risk: bool = False


@router.post("/oauth/start", summary="启动 MCP OAuth 登录")
async def start_oauth(
    body: OAuthStartRequest,
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = str(user.user_id)
    _require_can_add_mcp(user_id, db)
    market.get_market_item(
        db,
        body.slug,
        owner_user_id=user_id,
        viewer_user_id=user_id,
    )
    return success_response(
        data=await mcp_oauth_service.start_oauth_install(
            db,
            slug=body.slug,
            owner_user_id=user_id,
            installed_by=user_id,
            auth_method=body.auth_method,
            credentials=body.credentials,
            confirm_high_risk=body.confirm_high_risk,
            callback_url_base=mcp_oauth_service.public_oauth_callback_url(request),
            client_id=body.client_id,
            client_secret=body.client_secret,
        )
    )


@router.get("/oauth/status/{flow_id}", summary="查询 MCP OAuth 登录状态")
async def oauth_status(
    flow_id: str,
    user: UserContext = Depends(get_current_user),
):
    return success_response(
        data=await mcp_oauth_service.get_flow_status(flow_id, owner_user_id=str(user.user_id))
    )


@router.post("/oauth/cancel/{flow_id}", summary="取消 MCP OAuth 登录")
async def cancel_oauth(
    flow_id: str,
    user: UserContext = Depends(get_current_user),
):
    return success_response(
        data=await mcp_oauth_service.cancel_flow(
            flow_id,
            owner_user_id=str(user.user_id),
        )
    )


@router.get(
    "/oauth/callback",
    name="mcp_market_oauth_callback",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def oauth_callback(
    flow_id: str = Query(...),
    code: str = Query(""),
    state: Optional[str] = Query(None),
    error: str = Query(""),
    error_description: str = Query(""),
):
    await mcp_oauth_service.complete_callback(
        flow_id,
        code=code,
        state=state,
        error=error_description or error,
    )
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>OAuth</title>"
        "<p>OAuth 登录结果已返回 HugAgentOS，可以关闭此窗口。</p>"
        "<script>window.close()</script>"
    )


@router.post("/install", status_code=201, summary="安装市场 MCP 为私有实例")
async def install_item(
    body: InstallRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = str(user.user_id)
    _require_can_add_mcp(user_id, db)
    result = await market.install_market_item(
        db,
        body.slug,
        owner_user_id=user_id,
        installed_by=user_id,
        credentials=body.credentials,
        auth_method=body.auth_method,
        confirm_high_risk=body.confirm_high_risk,
    )
    return created_response(data=result)


class SubmissionRequest(BaseModel):
    source_server_id: str = Field(..., min_length=1, max_length=100)
    category: str = Field(..., min_length=1, max_length=64)
    version: str = Field("1.0.0", min_length=1, max_length=50)
    summary: str = Field("", max_length=2000)
    note: str = Field("", max_length=2000)
    tags: List[str] = Field(default_factory=list, max_length=20)


@router.post("/submissions", status_code=201, summary="申请把私有 MCP 上架市场")
async def submit_item(
    body: SubmissionRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = str(user.user_id)
    _require_can_add_mcp(user_id, db)
    result = await market.submit_to_marketplace(
        db,
        body.source_server_id,
        owner_user_id=user_id,
        submitter_name=user.username or "",
        category=body.category,
        version=body.version,
        summary=body.summary,
        note=body.note,
        tags=body.tags,
    )
    return created_response(data=result)


@router.get("/submissions", summary="我的 MCP 上架申请")
async def list_my_submissions(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(data={"items": market.list_my_submissions(db, str(user.user_id))})


@router.delete("/submissions/{submission_id}", summary="撤回 MCP 上架申请")
async def withdraw_submission(
    submission_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = str(user.user_id)
    _require_can_add_mcp(user_id, db)
    market.withdraw_submission(db, submission_id, user_id)
    return success_response(data={"submission_id": submission_id, "withdrawn": True})
