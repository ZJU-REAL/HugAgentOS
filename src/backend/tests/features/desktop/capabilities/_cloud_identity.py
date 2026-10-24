"""桌面混合模式测试共用的云端身份：一个账号、一份 token。

放在独立模块而不是 conftest：conftest 由 pytest 自己加载，被别的测试直接 import 时
容易变成两份实例。
"""

from __future__ import annotations

import base64
import json

CLOUD_BASE = "https://cloud.example"
CLOUD_USER = "u1"


def cloud_token(uid: str = CLOUD_USER) -> str:
    body = (
        base64.urlsafe_b64encode(
            json.dumps({"u": uid, "c": "center", "a": 1, "h": "session", "d": "device"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"dcap2.{body}.sig"
