"""对话模式 API。

两个面共用一套服务，靠**归属**分权，不靠两套代码：

* ``GET /v1/chat-modes`` —— 任何登录用户都能读自己可见的模式（官方 + 私有）。
  这是输入框上方那枚模式位的数据源。
* ``/v1/chat-modes/mine`` 下的增删改 —— 用户自建**私有**模式，需
  ``can_manage_chat_modes`` 权限位。
* ``/v1/chat-modes/official`` 下的增删改 —— **官方**模式（含内置的标准/极速），
  走 ADMIN_TOKEN，是 Config 台「模式选择」页的后端。

私有模式永远带 ``owner_user_id``，官方模式永远不带；服务层按这个字段校验归属，
路由层只负责把"我是谁"传下去。
"""

from typing import Any, Dict, List, Optional

from api.deps import require_admin_or_config
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models.chat_mode import ChatMode
from core.infra.logging import get_logger
from core.infra.responses import created_response, success_response
from core.services.chat_mode_service import ChatModeService
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/chat-modes", tags=["ChatModes"])


def _require_manage_cap(db: Session, user_id: str) -> None:
    from core.auth.capabilities import resolve_user_capabilities

    if not resolve_user_capabilities(db, user_id).get("can_manage_chat_modes", False):
        raise HTTPException(status_code=403, detail="无自定义模式权限（can_manage_chat_modes）")


class ChatModeIn(BaseModel):
    """模式的可写字段。除 name 外都可省略，省略即保持原值（更新）或取默认（创建）。"""

    name: Optional[str] = Field(None, description="模式名称", max_length=100)
    slug: Optional[str] = Field(None, description="模式标识；留空由名称派生", max_length=64)
    description: Optional[str] = Field(None, description="一句话说明它改了什么", max_length=500)
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None
    tool_scope: Optional[str] = Field(None, description="all=不收窄 / restricted=只用下面列出的")
    mcp_server_ids: Optional[List[str]] = None
    skill_ids: Optional[List[str]] = None
    plugin_ids: Optional[List[str]] = None
    agent_ids: Optional[List[str]] = Field(None, description="这个模式下可入场的子智能体")
    manual_invoke_enabled: Optional[bool] = None
    max_iters: Optional[int] = None
    default_effort: Optional[str] = Field(None, description="fast / medium / high / max")
    effort_locked: Optional[bool] = None
    #: 绑定提示词管理里的一个 tab（kind）；正文在那边编辑与版本化。
    prompt_kind: Optional[str] = Field(None, max_length=64)
    #: 就地手写的专属提示词正文。与 prompt_kind 互斥，两个都空则用默认装配。
    prompt_text: Optional[str] = None


def _mode_to_dict(row: ChatMode) -> Dict[str, Any]:
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "description": row.description or "",
        "is_builtin": bool(row.is_builtin),
        "is_private": row.owner_user_id is not None,
        "enabled": bool(row.enabled),
        "sort_order": row.sort_order,
        "tool_scope": row.tool_scope,
        "mcp_server_ids": row.mcp_server_ids or [],
        "skill_ids": row.skill_ids or [],
        "plugin_ids": row.plugin_ids or [],
        "agent_ids": row.agent_ids or [],
        "manual_invoke_enabled": bool(row.manual_invoke_enabled),
        "max_iters": row.max_iters,
        "default_effort": row.default_effort,
        "effort_locked": bool(row.effort_locked),
        "prompt_kind": row.prompt_kind,
        "prompt_text": row.prompt_text,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _public_mode_to_dict(row: ChatMode) -> Dict[str, Any]:
    """给对话框那枚模式位用的瘦身版：不下发工具/技能清单与提示词正文。

    模式的装配细节是服务端契约，前端只需要知道"叫什么、说明是什么、默认多深、
    能不能改强度"。少下发一层也少一个把内部 id 泄给前端的面。
    """
    return {
        "slug": row.slug,
        "name": row.name,
        "description": row.description or "",
        "is_builtin": bool(row.is_builtin),
        "is_private": row.owner_user_id is not None,
        "default_effort": row.default_effort,
        "effort_locked": bool(row.effort_locked),
    }


@router.get("", summary="当前用户可用的模式（对话框模式位数据源）")
async def list_modes(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ChatModeService(db)
    # 首次访问时补播内置行：老库升级上来不必额外跑脚本。
    service.ensure_builtins()
    rows = service.list_for_user(user.user_id)
    return success_response(data={"modes": [_public_mode_to_dict(r) for r in rows]})


@router.get("/options/mine", summary="我建模式时可选的能力清单")
async def my_capability_options(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """全量清单（不按启停过滤）+ 未安装的市场项，作用域是当前用户。"""
    _require_manage_cap(db, user.user_id)
    return success_response(data=ChatModeService(db).available_capabilities(user.user_id))


@router.get("/options/official", summary="官方模式可选的能力清单（Config 台）")
async def official_capability_options(
    _: None = Depends(require_admin_or_config),
    db: Session = Depends(get_db),
):
    """同上，但作用域是全局（不含任何人的私有技能 / 子智能体）。"""
    return success_response(data=ChatModeService(db).available_capabilities(None))


# ── 私有模式（用户设置页）────────────────────────────────────────────────


@router.get("/mine", summary="我自建的私有模式")
async def list_my_modes(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_manage_cap(db, user.user_id)
    rows = ChatModeService(db).list_owned(user.user_id)
    return success_response(data={"modes": [_mode_to_dict(r) for r in rows]})


@router.post("/mine", status_code=status.HTTP_201_CREATED, summary="新建私有模式")
async def create_my_mode(
    body: ChatModeIn,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_manage_cap(db, user.user_id)
    row = ChatModeService(db).create(body.model_dump(exclude_unset=True), owner_user_id=user.user_id)
    return created_response(data=_mode_to_dict(row))


@router.put("/mine/{mode_id}", summary="修改私有模式")
async def update_my_mode(
    mode_id: str,
    body: ChatModeIn,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_manage_cap(db, user.user_id)
    row = ChatModeService(db).update(
        mode_id, body.model_dump(exclude_unset=True), owner_user_id=user.user_id
    )
    return success_response(data=_mode_to_dict(row))


@router.delete("/mine/{mode_id}", summary="删除私有模式")
async def delete_my_mode(
    mode_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_manage_cap(db, user.user_id)
    ChatModeService(db).delete(mode_id, owner_user_id=user.user_id)
    return success_response(data={"deleted": mode_id})


# ── 官方模式（Config 台「模式选择」）──────────────────────────────────────


@router.get("/official", summary="官方模式列表（Config 台）")
async def list_official_modes(
    _: None = Depends(require_admin_or_config),
    db: Session = Depends(get_db),
):
    service = ChatModeService(db)
    service.ensure_builtins()
    return success_response(data={"modes": [_mode_to_dict(r) for r in service.list_official()]})


@router.post("/official", status_code=status.HTTP_201_CREATED, summary="新建官方模式")
async def create_official_mode(
    body: ChatModeIn,
    _: None = Depends(require_admin_or_config),
    db: Session = Depends(get_db),
):
    row = ChatModeService(db).create(body.model_dump(exclude_unset=True), owner_user_id=None)
    return created_response(data=_mode_to_dict(row))


@router.put("/official/{mode_id}", summary="修改官方模式")
async def update_official_mode(
    mode_id: str,
    body: ChatModeIn,
    _: None = Depends(require_admin_or_config),
    db: Session = Depends(get_db),
):
    row = ChatModeService(db).update(mode_id, body.model_dump(exclude_unset=True), owner_user_id=None)
    return success_response(data=_mode_to_dict(row))


@router.delete("/official/{mode_id}", summary="删除官方模式（内置的只能停用）")
async def delete_official_mode(
    mode_id: str,
    _: None = Depends(require_admin_or_config),
    db: Session = Depends(get_db),
):
    ChatModeService(db).delete(mode_id, owner_user_id=None)
    return success_response(data={"deleted": mode_id})
