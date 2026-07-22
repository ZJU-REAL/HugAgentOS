"""Personal sub-agent API for the community edition."""

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
    if not resolve_user_capabilities(db, user_id)["can_add_agent"]:
        raise AccessDeniedError(
            message="管理员未开放自建/安装子智能体功能",
            reason="can_add_agent_disabled",
        )


class AgentCreateRequest(BaseModel):
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


@router.get("", summary="列出当前用户可见的所有子智能体")
async def list_agents(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(data=UserAgentService(db).list_for_user(user.user_id))


@router.get("/available-resources", summary="可绑定到子智能体的资源列表")
async def available_resources(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    data = UserAgentService(db).list_available_resources(owner_user_id=str(user.user_id))
    return success_response(data=data)


@router.get("/{agent_id}", summary="子智能体详情")
async def get_agent(
    agent_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        agent = UserAgentService(db).get_by_id(agent_id, user_id=user.user_id)
    except LookupError:
        return error_response(code=404, message="Agent not found")
    except PermissionError:
        return error_response(code=403, message="Access denied")
    return success_response(data=agent)


@router.post("", summary="创建个人子智能体")
async def create_agent(
    body: AgentCreateRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_can_add_agent(str(user.user_id), db)
    try:
        agent = UserAgentService(db).create(
            user_id=user.user_id,
            operator_name=user.username,
            owner_type="user",
            data=body.model_dump(exclude_none=True),
        )
    except PermissionError as exc:
        return error_response(code=403, message=str(exc))
    except ValueError as exc:
        return error_response(code=400, message=str(exc))
    return success_response(data=agent)


@router.put("/{agent_id}", summary="更新个人子智能体")
async def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        agent = UserAgentService(db).update(
            agent_id,
            user_id=user.user_id,
            operator_name=user.username,
            owner_type="user",
            data=body.model_dump(exclude_none=True),
        )
    except LookupError:
        return error_response(code=404, message="Agent not found")
    except PermissionError:
        return error_response(code=403, message="Access denied")
    return success_response(data=agent)


@router.delete("/{agent_id}", summary="删除个人子智能体")
async def delete_agent(
    agent_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        UserAgentService(db).delete(agent_id, user_id=user.user_id, owner_type="user")
    except LookupError:
        return error_response(code=404, message="Agent not found")
    except PermissionError:
        return error_response(code=403, message="Access denied")
    return success_response(data={"deleted": True})


__all__ = ["AgentCreateRequest", "AgentUpdateRequest", "router"]
