"""Personal sub-agent API for the community edition."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError, BadRequestError
from core.infra.responses import error_response, success_response
from core.services.agent_markdown import agent_filename, agent_to_markdown, parse_agents_upload
from core.services.user_agent_service import UserAgentService
from fastapi import APIRouter, Depends, File, Response, UploadFile
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


_IMPORT_FIELDS = [
    "name",
    "avatar",
    "description",
    "system_prompt",
    "welcome_message",
    "suggested_questions",
    "mcp_server_ids",
    "skill_ids",
    "plugin_ids",
    "kb_ids",
    "model_provider_id",
    "temperature",
    "max_tokens",
    "max_iters",
    "timeout",
    "is_enabled",
    "extra_config",
]


@router.post("/import", summary="导入子智能体（markdown / zip）")
async def import_agents(
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """从上传文件导入个人子智能体：单个 frontmatter markdown（.md）或批量 zip。

    文件格式与导出一致（frontmatter 写配置、正文即 system prompt），也兼容旧版
    JSON 数组文件。需 ``can_add_agent`` 权限。
    """
    _require_can_add_agent(str(user.user_id), db)
    raw = await file.read()
    try:
        items = parse_agents_upload(file.filename or "", raw)
    except ValueError as exc:
        raise BadRequestError(str(exc))

    svc = UserAgentService(db)
    agents = []
    for item in items:
        data = {k: item[k] for k in _IMPORT_FIELDS if k in item}
        try:
            agents.append(
                svc.create(
                    user_id=user.user_id,
                    operator_name=user.username,
                    owner_type="user",
                    data=data,
                )
            )
        except ValueError as exc:
            return error_response(code=400, message=f"{item.get('name')}: {exc}")
    logger.info("user_agents_imported: user=%s created=%d", user.user_id, len(agents))
    return success_response(data={"created": len(agents), "agents": agents})


@router.get("/{agent_id}/export", summary="导出子智能体为 markdown")
async def export_agent(
    agent_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把单个子智能体导出为 frontmatter markdown 文件（正文即 system prompt），
    对齐 Claude Code / pi agent 一类 harness 的 subagent 文件格式，可直接重新导入。
    """
    from core.llm.builtin_subagents import list_builtin_subagents

    agent = next(
        (
            item
            for item in list_builtin_subagents(include_prompt=True)
            if item["agent_id"] == agent_id
        ),
        None,
    )
    if agent is None:
        svc = UserAgentService(db)
        try:
            agent = svc.get_by_id(agent_id, user_id=user.user_id)
        except LookupError:
            return error_response(code=404, message="Agent not found")
        except PermissionError:
            return error_response(code=403, message="Access denied")

    text = agent_to_markdown(agent)
    fname = agent_filename(str(agent.get("name") or "agent"))
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=\"agent.md\"; filename*=UTF-8''{quote(fname)}"
        },
    )


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
