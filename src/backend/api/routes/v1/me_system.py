"""Personal system settings API (/v1/me/system) —— the service-config surface delegated in CE.

The CE derived tree has no Config system console (`service_configs.py` is EE, physically absent),
so deployment-level service configs such as the search-engine key have no UI to write in CE. This
route delegates a whitelist of groups **related to personal use** (internet search / file parsing /
knowledge base / sandbox / context) to the instance admin (gate = ``require_system_settings``:
CONFIG_TOKEN / can_system_config capability bit / CE+mock single trust domain). It is equally usable
under EE, but the frontend only shows the entry in CE (EE uses the Config console, to avoid a double entry).

Enterprise-governance groups (dingtalk / lark / auth / industry, etc.) are **not** in the whitelist,
and remain exclusive to the EE system console.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from api.deps import require_system_settings, user_can_manage_system_settings
from core.auth.backend import UserContext, require_auth
from core.config.settings import settings
from core.db.engine import get_db
from core.infra.responses import success_response
from core.services.service_probes import (
    config_row_to_dict,
    reinitialize_mcp_pool,
    test_service_group,
)
from core.services.system_config import SystemConfigService
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/me/system", tags=["My System Settings"])
logger = logging.getLogger(__name__)

# Whitelist of service-config groups related to personal use (key → display name).
# Enterprise workbench (dingtalk/lark), login session (auth), and EE industry services (industry) are not delegated.
PERSONAL_GROUPS: Dict[str, str] = {
    "internet_search": "互联网搜索",
    "file_parser": "文件解析服务",
    "knowledge_base": "知识库服务",
    "sandbox": "沙箱 / 代码执行",
    "context": "对话上下文压缩",
}

# Whitelist groups that support connectivity testing (sandbox/context are pure switches, no external service to test)
_TESTABLE_GROUPS = {"internet_search", "file_parser", "knowledge_base"}


def _personal_groups() -> Dict[str, str]:
    """Return the edition-specific settings surface.

    CE provides only owner-isolated local knowledge bases, so the shared/Dify
    knowledge-base connector is not configurable there.
    """
    if settings.edition.edition == "ce":
        return {key: label for key, label in PERSONAL_GROUPS.items() if key != "knowledge_base"}
    return PERSONAL_GROUPS


class ConfigUpdateItem(BaseModel):
    key: str
    value: Optional[str] = None


class BulkUpdateRequest(BaseModel):
    items: List[ConfigUpdateItem]


def _svc() -> SystemConfigService:
    return SystemConfigService.get_instance()


# ── Access probe ─────────────────────────────────────────────────────────────────


@router.get("/access", summary="个人系统设置访问探针（不抛 403）")
async def system_settings_access(
    user: Optional[UserContext] = Depends(require_auth(False)),
    db: Session = Depends(get_db),
):
    """前端据此显隐「设置 → 系统管理」入口；只返回布尔与版本，不泄露配置内容。"""
    allowed = bool(user) and user_can_manage_system_settings(db, user.user_id)
    return success_response(data={"allowed": allowed, "edition": settings.edition.edition})


# ── Service configs (whitelist groups) ───────────────────────────────────────────────────


@router.get("/service-configs", summary="列出个人相关服务配置（白名单分组）")
async def list_personal_configs(_: str = Depends(require_system_settings)):
    """按分组返回白名单内的服务配置项；密钥类配置已脱敏。"""
    configs = _svc().get_all_configs()
    personal_groups = _personal_groups()
    grouped: Dict[str, dict] = {}
    for gk, label in personal_groups.items():
        grouped[gk] = {
            "group_key": gk,
            "label": label,
            "testable": gk in _TESTABLE_GROUPS,
            "items": [],
        }
    for cfg in configs:
        gk = cfg.get("group_key")
        if gk in grouped:
            grouped[gk]["items"].append(config_row_to_dict(cfg))
    return success_response(data=list(grouped.values()))


@router.put("/service-configs", summary="批量更新个人相关服务配置")
async def update_personal_configs(
    body: BulkUpdateRequest,
    _: str = Depends(require_system_settings),
):
    """按 key 批量写入；key 必须落在白名单分组内，越界返回 400。

    写入后异步重建 MCP 连接池，让搜索等 MCP 子进程拿到新 key（≤30s 生效）。
    掩码值（含 ``****``）由底层 ``bulk_set`` 跳过，防止把脱敏串写回真实密钥。
    """
    if not body.items:
        raise HTTPException(status_code=400, detail="items cannot be empty")
    svc = _svc()
    personal_groups = _personal_groups()
    key_to_group = {c["config_key"]: c.get("group_key") for c in svc.get_all_configs()}
    for item in body.items:
        group = key_to_group.get(item.key.strip())
        if group not in personal_groups:
            raise HTTPException(
                status_code=400,
                detail=f"配置项 {item.key} 不在个人可配置范围内",
            )
    svc.bulk_set([item.model_dump() for item in body.items], updated_by="me_system")
    asyncio.ensure_future(reinitialize_mcp_pool("me_system"))
    return success_response(data={"updated": len(body.items)})


@router.post("/service-configs/test/{group_key}", summary="测试服务连通性")
async def test_personal_config(
    group_key: str,
    _: str = Depends(require_system_settings),
):
    """对白名单分组做一次实际连通性测试（互联网搜索测 Tavily/百度，文件解析/知识库做健康检查）。"""
    if group_key not in _personal_groups():
        raise HTTPException(status_code=404, detail=f"Unknown group: {group_key}")
    if group_key not in _TESTABLE_GROUPS:
        raise HTTPException(status_code=400, detail="该分组无可测试的外部服务")
    return success_response(data=await test_service_group(group_key))
