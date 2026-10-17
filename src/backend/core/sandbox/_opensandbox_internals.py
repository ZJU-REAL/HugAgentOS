"""OpenSandbox provider internals — module-level helpers, manifest parsing,
session/pool helper classes. Extracted from opensandbox_provider.py to slim it;
the OpenSandboxProvider class imports these back. No dependency on the provider
class itself (no cycle).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from core.config.settings import settings

from ._common import (
    SANDBOX_RUN_GID,
    SANDBOX_RUN_UID,
    WORKSPACE,
    dws_cache_dir,
    dws_extra_envs,
    email_cache_dir,
    lark_cache_dir,
    myspace_cache_dir,
    safe_user_id,
    shell_escape,
    yida_cache_dir,
)
from .errors import SandboxError


def _get_db_session_local():
    from core.db.engine import SessionLocal
    return SessionLocal


def _get_snapshot_model():
    from core.db.models import ChatSandboxSnapshot
    return ChatSandboxSnapshot

logger = logging.getLogger(__name__)

# Backward-compatible private alias used by the existing OpenSandbox/Cube
# execution paths.  The implementation is provider-neutral so CE callers do
# not need to import this EE-only module.
_dws_extra_envs = dws_extra_envs


async def write_file(sbx: Any, path: str, content: Any) -> None:
    """把文件写进沙箱，并把新建的路径归还给运行用户。

    execd 的 files API 固定以 root 身份写入，途中新建的父目录是 root:root 755；
    而用户代码以 uid 1000 执行，之后往同一个目录写相对路径的输出文件就会 EACCES
    （典型现象：上传的 xlsx 落到 /workspace/dialog/ 后，脚本 cd 过去写 rows.json 报
    PermissionError）。所有进沙箱的写入都必须走这里，不要直接调 files.write_file。
    """
    await sbx.files.write_file(path, content)

    prefix = WORKSPACE.rstrip("/") + "/"
    cur = PurePosixPath(path)
    targets = []
    while cur.as_posix().startswith(prefix):
        targets.append(shell_escape(cur.as_posix()))
        cur = cur.parent
    if not targets:
        return
    cmd = f"chown {SANDBOX_RUN_UID}:{SANDBOX_RUN_GID} -- {' '.join(targets)}"
    try:
        from opensandbox.models.execd import RunCommandOpts

        await asyncio.wait_for(
            sbx.commands.run(cmd, opts=RunCommandOpts(timeout=timedelta(seconds=5))),
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[opensandbox] chown %s 失败（继续）: %r", path, exc)


def _user_bound_sandbox_required() -> bool:
    """Whether persistent OpenSandbox sessions need per-user containers.

    Every enabled private-data bind mount makes a sandbox user-specific. Such a
    sandbox must never come from the general pool: general-pool instances are
    created without ``user_id`` and therefore have none of these volumes. Using
    one would either hide the current user's credentials (DingTalk/Feishu/email/
    Yida report "not logged in") or risk reusing another user's mounted data.

    This decision intentionally does not depend on the MySpace switch alone.
    Deployments may disable the MySpace optimization while still relying on one
    or more account integrations — or on nothing but the skill view, which is
    per-user as soon as there is host storage to bind (a general-pool sandbox
    would then be missing the user's own private skills).
    """
    sandbox = settings.sandbox
    return any(
        (
            bool((sandbox.opensandbox_host_storage_path or "").strip()),
            sandbox.opensandbox_myspace_bind_mount_enabled,
            sandbox.dws_creds_bind_mount_enabled,
            sandbox.lark_creds_bind_mount_enabled,
            sandbox.email_creds_bind_mount_enabled,
            sandbox.yida_creds_bind_mount_enabled,
        )
    )

# OpenSandbox snapshot lifecycle states (the status.state returned by GET /v1/snapshots/{id}).
# SDK 0.1.13 does not expose an enum, so we can only string-compare these values.
_SNAPSHOT_STATE_READY = "Ready"
_SNAPSHOT_TERMINAL_FAILURE_STATES = frozenset({"Failed", "Error"})

# Entrypoints earlier versions created sandboxes with. Nothing is created with them
# any more (every sandbox uses _LIGHT_ENTRYPOINT), but adopt still recognizes them so
# orphans left by an older backend get killed instead of leaking.
_LEGACY_ENTRYPOINTS = (
    ["/opt/opensandbox/code-interpreter.sh"],
    ["/opt/hugagent/entrypoint-as-ubuntu.sh"],
)

# Plan F Q1: environment variables used when running bash via sandbox.commands.run (execd).
# execd does not source /opt/opensandbox/code-interpreter-env.sh; PATH then only contains the
# system bin paths, so python resolves to a "system python" like /usr/bin/python3 that has no
# pandas/docx etc. installed. We explicitly provide a PATH matching where packages are
# installed for PY_BIN=3.14.2 in Dockerfile.opensandbox, plus the node/go/java toolchains.
# 3.14.2 is the pip install target in the Dockerfile.
_EXECD_BASH_ENVS: dict[str, str] = {
    "PATH": (
        "/opt/python/current/bin:"
        "/opt/node/v22.2.0/bin:"
        "/opt/go/1.25.5/bin:"
        "/usr/lib/jvm/java-21-openjdk-amd64/bin:"
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    ),
    "HOME": "/home/ubuntu",
    "USER": "ubuntu",
    "LOGNAME": "ubuntu",
    "JAVA_HOME": "/usr/lib/jvm/java-21-openjdk-amd64",
    "NODE_PATH": "/opt/node/v22.2.0/lib/node_modules",
    "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
}
# The ephemeral path does not start Jupyter; the injected execd alone provides commands.run.
# Startup drops from ~10s to ~3s.
_LIGHT_ENTRYPOINT = ["sleep", "infinity"]

# 用户绑定沙箱的 entrypoint：容器启动时就建好目录约定（scratch + /myspace 软链），
# 首条命令不必再为此花一次 execd 往返（实测固定 ~1s，而这段 shell 只要 2ms）。uid 走
# 环境变量，entrypoint 保持常量，adopt 仍能按它认出自己创建的沙箱。
#
# 两个约束：``/myspace`` 已是真目录时不能直接 ln（软链会建到目录里面），所以先判断；
# 整段不许中途退出，任何一步失败都要走到 exec sleep infinity，否则容器直接死掉。
_SETUP_ENTRYPOINT = [
    "/bin/sh",
    "-c",
    "mkdir -p /workspace/scratch 2>/dev/null; "
    'if [ -n "$JX_MYSPACE_UID" ]; then '
    '[ -d "/workspace/myspace/$JX_MYSPACE_UID" ] || '
    f"install -d -o {SANDBOX_RUN_UID} -g {SANDBOX_RUN_GID} "
    '"/workspace/myspace/$JX_MYSPACE_UID" 2>/dev/null; '
    "if [ -L /myspace ] || [ ! -e /myspace ]; then "
    'rm -f /myspace; ln -s "/workspace/myspace/$JX_MYSPACE_UID" /myspace 2>/dev/null; '
    "fi; "
    "fi; "
    "exec sleep infinity",
]

@dataclass
class _Session:
    sandbox: Any  # opensandbox.Sandbox
    interpreter: Any  # code_interpreter.CodeInterpreter
    contexts: dict[str, Any]  # language → CodeContext (one ctx per language, mutually isolated)
    seeded_myspace_mtime: dict[str, float]  # filename → mtime (for incremental sync)
    # The user_id owning this sandbox (binds MySpace/account-credential volumes +
    # user re-homing on adopt across restarts). None when legacy paths call
    # _get_or_create_session without a user_id — execution then falls back to a
    # credential-less general sandbox.
    user_id: Optional[str] = None
    # Plan F: which pool this sandbox was actually taken from. **release must follow this,
    # never re-decide from the current user-bound-volume settings** — a mid-flight
    # flag change (ops toggle, hot reload) would route release to a different pool,
    # corrupting counts / cross-user contamination. The snapshot restore path builds
    # directly via _create_jupyter_sandbox without any pool acquire ("none"); on destroy it
    # destroys outright instead of calling either pool's release — calling _pool.release
    # would push _pool.in_flight negative (no paired acquire), and calling
    # _jupyter_user_pool.release would leak the previous chat's /workspace/scratch to the
    # same user's next chat. Three states: 'user' | 'general' | 'none'
    pool_source: str = "none"
    setup_done: bool = False  # whether the directory conventions (scratch/myspace/symlinks) are set up
    # 已建好 /myspace 软链的 uid。不带 user_id 的调用方会让 setup 跳过 myspace 那一半，
    # 只看 setup_done 会把残缺状态当成完成，并随沙箱回到空闲池污染后续会话。
    myspace_link_uid: Optional[str] = None
    last_renew_ts: float = 0.0
    # Monotonic clock of the last business request (execute/put_file/get_file). Used for
    # idle reaping: a background task proactively destroys persistent sessions whose
    # now - last_active_ts exceeds the threshold, instead of waiting for the server TTL.
    last_active_ts: float = 0.0
    # Set True when the background async renew reports a lifecycle signal
    # (404/422/terminated); the next _get_or_create_session destroys and rebuilds the
    # moment it takes the lock, avoiding a 3s synchronous renew on the request path.
    stale_marked: bool = False
    # Consecutive renew failure count: used to escalate "persistent ReadTimeout /
    # ConnectError" to stale as well. A single timeout may be transient server-side high
    # load and should not destroy a healthy session; but N consecutive failures usually
    # mean the sandbox is actually dead while the server still lists it as existing (the
    # window before the 404). A successful _renew resets it to 0. See
    # _renew_in_background for the N threshold.
    consecutive_renew_failures: int = 0


def _join_logs(parts: list) -> str:
    """The SDK's result.logs.stdout/stderr is list[StreamItem]; StreamItem.text is a string.

    Each item is one output line **without** its trailing newline, so a plain
    ``"".join`` glues every line together — ``find``/``ls``/``md5sum`` output
    comes back as one unbroken string and any caller that parses it line by line
    (Glob, Grep, the bash → My Space write-back) reads the whole listing as a
    single bogus entry. Restore the boundary, leaving items that already carry
    their own newline untouched.

    "一项 = 完整一行" 是 execd 侧的行为，实测成立：20 万字符的单行也没有被缓冲
    区切开，所以补换行不会把一行拦腰截断。

    空行由 execd 单独发一条内容为 ``"\n"`` 的事件（v1.0.17 起，上游 c1d19b7c）。
    "自带换行的不再补" 这一条正是为它留的：两者合起来逐字节还原
    （``A``、``\n``、``\n``、``B`` → ``A\n\n\nB\n``），补重了空行会翻倍。
    execd 早于 v1.0.17 时根本不为空行发事件，那种情况下空行在到达后端之前就没了，
    这里补不回来——版本钉在 ``docker/opensandbox-config.toml.tpl``。
    """
    if not parts:
        return ""
    out = []
    for p in parts:
        text = getattr(p, "text", None)
        if text is None:
            text = str(p)
        out.append(text if text.endswith("\n") else text + "\n")
    return "".join(out)

from ._opensandbox_volumes import (
    _BUILTIN_SKILLS_HOST_PATH,
    _resolve_builtin_skills_host_path,
    _METADATA_OWNER_KEY,
    _METADATA_OWNER_VALUE,
    _METADATA_POOL_KIND_KEY,
    _METADATA_USER_ID_KEY,
    _METADATA_SKILLS_LAYOUT_KEY,
    _METADATA_SKILLS_LAYOUT_VALUE,
    _pool_metadata,
    _make_skills_volumes,
    _make_myspace_volume,
)

from ._opensandbox_account_volumes import (
    _DWS_HOME,
    _DWS_CREDS_SUBPATHS,
    _make_dws_creds_volumes,
    _firecrawl_extra_envs,
    _LARK_HOME,
    _LARK_CREDS_SUBPATHS,
    _make_lark_creds_volumes,
    _EMAIL_HOME,
    _EMAIL_CREDS_SUBPATHS,
    _make_email_creds_volumes,
    _YIDA_WORKSPACE_MOUNT,
    _YIDA_CREDS_SUBPATHS,
    _make_yida_creds_volumes,
)

from ._opensandbox_user_pool import (
    _JupyterUserPool,
)
