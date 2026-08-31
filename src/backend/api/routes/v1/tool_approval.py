"""工具执行权限档 API（/v1/tool-approval）。

输入框工具栏上那颗权限胶囊的数据源：逐项确认 / 替我批准 / 完全放开。档位是**每个用户一份**，
存在 ``users_shadow.metadata.tool_approval_mode``，每次对话装配时读出来交给
``core.llm.tool_permissions`` 决定要不要为工具调用停下来问用户。

网页端与桌面端共用这一档——桌面端不再另存一份「本机操作权限档」，本机执行策略由
``core.services.local_grant_service.policy_for_gate()`` 按本档位翻译。桌面端仍有的
「授权目录 / 危险命令分类处置」（``/v1/local/grants``、``/v1/local/policy``）管的是
"本机哪些目录能动"，与本档位是两件事。
"""

from __future__ import annotations

from typing import Literal

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.responses import success_response
from core.llm.tool_permissions import normalize_approval_mode
from core.services.user_service import UserService
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/tool-approval", tags=["Tool Approval"])

_METADATA_KEY = "tool_approval_mode"


class ApprovalModeBody(BaseModel):
    mode: Literal["ask", "auto", "full"]


@router.get("", summary="获取当前工具执行权限档")
async def get_tool_approval_mode(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = UserService(db).get_user_settings(str(user.user_id))
    return success_response(data={"mode": normalize_approval_mode(settings.get(_METADATA_KEY))})


@router.put("", summary="设置工具执行权限档")
async def set_tool_approval_mode(
    body: ApprovalModeBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    mode = normalize_approval_mode(body.mode)
    UserService(db).update_user_metadata(user_id=str(user.user_id), patch={_METADATA_KEY: mode})
    return success_response(data={"mode": mode}, message="已切换权限档")
