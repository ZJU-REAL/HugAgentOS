"""User-facing sub-agent API routes.

Provides CRUD for user-owned agents and read access to admin agents.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError
from core.infra.responses import error_response, success_response
from core.services.user_agent_service import UserAgentService
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/agents", tags=["User Agents"])
logger = logging.getLogger(__name__)


def _require_can_add_agent(user_id: str, db: Session) -> None:
    # personal explicit (user management) → team default (team management) → off by default
    if not resolve_user_capabilities(db, user_id)["can_add_agent"]:
        raise AccessDeniedError(
            message="管理员未开放自建/安装子智能体功能", reason="can_add_agent_disabled"
        )


# ── Pydantic schemas ─────────────────────────────────────────────────────────


class AgentCreateRequest(BaseModel):
    # passing team_id = create a team sub-agent (requires being owner/admin of that team); omitting = personal sub-agent
    team_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=255)
    avatar: Optional[str] = None
    description: Optional[str] = Field("", max_length=20)
    system_prompt: str = ""
    welcome_message: Optional[str] = ""
    suggested_questions: Optional[List[str]] = Field(default_factory=list)
    mcp_server_ids: Optional[List[str]] = Field(default_factory=list)
    skill_ids: Optional[List[str]] = Field(default_factory=list)
    plugin_ids: Optional[List[str]] = Field(default_factory=list)
    kb_ids: Optional[List[str]] = Field(default_factory=list)
    model_provider_id: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_iters: Optional[int] = 10
    timeout: Optional[int] = 120
    ontology_tags: Optional[List[str]] = Field(default_factory=list)
    extra_config: Optional[Dict[str, Any]] = Field(default_factory=dict)


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    avatar: Optional[str] = None
    description: Optional[str] = Field(None, max_length=20)
    system_prompt: Optional[str] = None
    welcome_message: Optional[str] = None
    suggested_questions: Optional[List[str]] = None
    mcp_server_ids: Optional[List[str]] = None
    skill_ids: Optional[List[str]] = None
    plugin_ids: Optional[List[str]] = None
    kb_ids: Optional[List[str]] = None
    model_provider_id: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_iters: Optional[int] = None
    timeout: Optional[int] = None
    is_enabled: Optional[bool] = None
    ontology_tags: Optional[List[str]] = None
    extra_config: Optional[Dict[str, Any]] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", summary="列出当前用户可见的所有子智能体")
async def list_agents(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出当前用户可见的所有子智能体（含本人创建的与管理员发布的）。需登录。"""
    svc = UserAgentService(db)
    agents = svc.list_for_user(user.user_id)
    return success_response(data=agents)


@router.get("/available-resources", summary="可绑定到子智能体的资源列表")
async def available_resources(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出可绑定到子智能体的资源（MCP 工具、技能、知识库等），供创建/编辑时选择。需登录。"""
    svc = UserAgentService(db)
    resources = svc.list_available_resources(owner_user_id=str(user.user_id))
    return success_response(data=resources)


@router.get("/{agent_id}", summary="子智能体详情")
async def get_agent(
    agent_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取指定子智能体的详情。不存在返回 404，无权访问返回 403。需登录。"""
    svc = UserAgentService(db)
    try:
        agent = svc.get_by_id(agent_id, user_id=user.user_id)
    except LookupError:
        return error_response(code=404, message="Agent not found")
    except PermissionError:
        return error_response(code=403, message="Access denied")
    return success_response(data=agent)


@router.post("", summary="创建子智能体（个人 / 团队）")
async def create_agent(
    body: AgentCreateRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建子智能体（名称、提示词、绑定的工具/技能/知识库等）。

    - 不传 ``team_id`` → 个人子智能体（owner_type=user，仅本人可见）。
    - 传 ``team_id`` → 团队子智能体（owner_type=team，对团队成员可见可用），仅该团队
      owner/admin 可创建，否则返回 403。参数校验失败返回 400。需登录。

    需 ``can_add_agent`` 权限（与技能/MCP 自助一致，由 Config 后管按用户/团队开放）。
    团队子智能体在此之上仍需该团队 owner/admin 身份。
    """
    _require_can_add_agent(str(user.user_id), db)
    svc = UserAgentService(db)
    data = body.model_dump(exclude_none=True)
    team_id = data.pop("team_id", None)
    owner_type = "team" if team_id else "user"
    try:
        agent = svc.create(
            user_id=user.user_id,
            operator_name=user.username,
            owner_type=owner_type,
            data=data,
            team_id=team_id,
        )
    except PermissionError as exc:
        return error_response(code=403, message=str(exc))
    except ValueError as exc:
        return error_response(code=400, message=str(exc))
    return success_response(data=agent)


@router.put("/{agent_id}", summary="更新用户子智能体")
async def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新指定子智能体（仅传入字段被修改）。不存在 404，无权修改 403。需登录。"""
    svc = UserAgentService(db)
    data = body.model_dump(exclude_none=True)
    try:
        agent = svc.update(
            agent_id,
            user_id=user.user_id,
            operator_name=user.username,
            owner_type="user",
            data=data,
        )
    except LookupError:
        return error_response(code=404, message="Agent not found")
    except PermissionError:
        return error_response(code=403, message="Access denied")
    return success_response(data=agent)


@router.delete("/{agent_id}", summary="删除用户子智能体")
async def delete_agent(
    agent_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除指定子智能体。不存在 404，无权删除 403。需登录。"""
    svc = UserAgentService(db)
    try:
        svc.delete(agent_id, user_id=user.user_id, owner_type="user")
    except LookupError:
        return error_response(code=404, message="Agent not found")
    except PermissionError:
        return error_response(code=403, message="Access denied")
    return success_response(data={"deleted": True})
