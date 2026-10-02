"""Persisted sidebar ordering and order normalization."""

from typing import List, Optional

import api.routes.v1.chats.models as chat_models
import api.routes.v1.chats.session_context as chat_session_context
import core.auth.backend as auth_backend
import core.db.engine as db_engine
import core.services as chat_services
from core.auth.backend import UserContext
from core.infra.responses import success_response
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

router = APIRouter()


SIDEBAR_ORDER_KEY = "sidebar_chat_order"


SIDEBAR_ORDER_MAX = 500


def _dedup_id_list(raw: Optional[list]) -> List[str]:
    """Clean + de-duplicate an id list, preserving first-seen order."""
    seen: set = set()
    out: List[str] = []
    for cid in chat_session_context._clean_id_list(raw):
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


@router.get("/sidebar-order", summary="获取侧边栏手动排序")
def get_sidebar_order(
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """侧边栏对话列表的手动拖拽顺序（chat_id 序列）。

    没拖过的账号返回空数组——前端据此退回「置顶 + 最近更新」默认排序。

    注意：本路由必须声明在 ``GET /{chat_id}`` **之前**，否则会被 path 参数
    路由吞掉（FastAPI 按声明顺序匹配）。
    """
    user_settings = chat_services.UserService(db).get_user_settings(str(user.user_id))
    order = _dedup_id_list(user_settings.get(SIDEBAR_ORDER_KEY))[:SIDEBAR_ORDER_MAX]
    return success_response(data={"order": order})


@router.put("/sidebar-order", summary="保存侧边栏手动排序")
def update_sidebar_order(
    request: chat_models.UpdateSidebarOrderRequest,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """整表覆盖写入手动顺序；空数组 = 恢复默认排序。

    不校验 chat_id 是否存在：顺序表是纯 UI 偏好，已删除的会话留在表里也只是
    查不到对应项而被忽略，反倒省掉一次全表校验。超出上限的尾部直接截断。
    """
    order = _dedup_id_list(request.order)[:SIDEBAR_ORDER_MAX]
    chat_services.UserService(db).update_user_metadata(
        user_id=str(user.user_id), patch={SIDEBAR_ORDER_KEY: order}
    )
    return success_response(data={"order": order})
