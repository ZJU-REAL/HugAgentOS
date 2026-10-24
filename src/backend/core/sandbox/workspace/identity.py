"""Execution identities and user-id validation shared by sandbox providers."""

from __future__ import annotations

import re

# 沙盒内运行用户代码的身份。与 backend 容器的 appuser 同为 UID 1000，这样 bind mount
# 进来的 myspace 目录两边同属主，沙盒写的文件 backend 能覆盖和删除。
# 镜像里 /workspace、/home/ubuntu 也归这个 UID（见 docker/sandbox/Dockerfile.opensandbox）。
SANDBOX_RUN_UID = 1000

SANDBOX_RUN_GID = 1000

USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def safe_user_id(user_id: str | None) -> str:
    """Return the value unchanged if valid; return an empty string if invalid/empty.
    Uniform replacement for the scattered ``uid if uid and USER_ID_RE.match(uid) else ""`` idiom."""
    return user_id if user_id and USER_ID_RE.match(user_id) else ""
