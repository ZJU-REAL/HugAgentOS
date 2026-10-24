"""OpenSandbox execution environment, launch commands and SDK file helpers."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import PurePosixPath
from shlex import quote as shell_escape
from typing import Any

from core.sandbox.workspace.identity import SANDBOX_RUN_GID, SANDBOX_RUN_UID
from core.sandbox.workspace.paths import WORKSPACE

logger = logging.getLogger(__name__)


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


# Environment variables used when running bash via sandbox.commands.run (execd).
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
# PID 1 is sandbox-init (docker/sandbox/sandbox-init.c): it reaps every orphan and kills
# what user commands, which run as SANDBOX_RUN_UID, leave behind.
_LIGHT_ENTRYPOINT = ["sandbox-init", str(SANDBOX_RUN_UID)]

# 用户绑定沙箱的 entrypoint：容器启动时就建好目录约定（scratch + /myspace 软链），
# 首条命令不必再为此花一次 execd 往返（实测固定 ~1s，而这段 shell 只要 2ms）。uid 走
# 环境变量，entrypoint 保持常量，adopt 仍能按它认出自己创建的沙箱。
#
# 两个约束：``/myspace`` 已是真目录时不能直接 ln（软链会建到目录里面），所以先判断；
# 整段不许中途退出，任何一步失败都要走到 exec sandbox-init，否则容器直接死掉。
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
    f"exec sandbox-init {SANDBOX_RUN_UID}",
]


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
    这里补不回来——版本钉在 ``docker/opensandbox-server/opensandbox-config.toml.tpl``。
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
