"""Lark state responsibilities."""
from __future__ import annotations
import asyncio
from typing import Any, Dict, Optional

LOGIN_TASKS: "dict[str, asyncio.Task]" = {}


LARK_BIN = "lark-cli"


DEFAULT_LOGIN_ARGS = ["auth", "login", "--recommend", "--no-wait", "--json"]


CLEAR_LOGIN: Dict[str, Any] = {
    "login_verification_url": None,
    "login_verification_url_complete": None,
    "login_user_code": None,
    "login_device_code": None,
}


EXPIRED_ERROR = "飞书登录已失效（令牌被刷新或过期），请到设置页重新扫码连接飞书"


NO_APP_ERROR = "飞书应用尚未配置，请联系管理员在「插件 → 飞书工作台」初始化飞书应用"


AUTH_FAIL_MARKERS = (
    "expired_token", "access_denied", "invalid_grant", "invalid_device_code",
    "authorization_expired", "expired", "denied",
)


PENDING_MARKERS = ("authorization_pending", "pending", "slow_down")


APP_CFG_SUB = ".lark-cli/config.json"


APP_STORE_SUB = ".local/share/lark-cli"


APP_INIT_TASK: "Optional[asyncio.Task]" = None


app_init_state: Dict[str, Any] = {
    "status": "idle", "verification_url": None, "qr_data_uri": None, "error": None,
}
