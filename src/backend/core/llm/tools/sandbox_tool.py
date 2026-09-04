"""Sandbox-backed agent tools: ``bash`` + artifact staging.

- ``bash``: run a shell command inside the per-chat sandbox container.
- ``sandbox_put_artifact``: copy an existing artifact's bytes into the sandbox.
- ``sandbox_get_artifact``: read a sandbox file and register it as a
  downloadable artifact.

Relocated from the former ``core.llm.tool`` module so the singular ``tool.py``
no longer coexists with this ``tools/`` package.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse
from core.llm.tools._common import resolve_sandbox_session
from core.llm.tools._tool_helpers import (
    _resolve_artifact_files,
    _resp_json,
    _store_generated_file_path,
    _validate_workspace_path,
)

logger = logging.getLogger(__name__)

import re as _re

# dws exit code 4 = PAT authorization interception; stderr/stdout carries a line
# ``PAT_AUTHORIZATION_URL=<url>`` (a copy-safe link dws prints separately for
# OpenClaw-style hosts, see dws CHANGELOG #242).
_PAT_URL_RE = _re.compile(r"PAT_AUTHORIZATION_URL=(\S+)")


def _detect_dws_pat_authorization(exit_code: int, stdout: str, stderr: str) -> Optional[dict]:
    """Detect a dws PAT per-scope authorization interception and return a structured hint; return None otherwise.

    Pure function for easy unit testing. Hit condition: ``PAT_AUTHORIZATION_URL=``
    can be extracted from the output — exit code 4 alone is not enough (4 could
    also be some other validation error); the presence of the link is decisive.
    """
    blob = f"{stdout or ''}\n{stderr or ''}"
    m = _PAT_URL_RE.search(blob)
    if not m:
        return None
    return {
        "authorization_url": m.group(1).rstrip(".,;"),
        "exit_code": exit_code,
        "reason": "dingtalk_pat_consent_required",
    }


async def _pull_myspace_updates(user_id: str) -> None:
    """执行 bash 前把「我的空间」的最新状态落进镜像目录，命令看到的就是用户当下的文件。

    界面上的上传、改名、删除只动 artifact 记录，不碰镜像目录；不补这一步，``ls /myspace``
    看到的就是过期视图 —— 用户刚传的看不见，刚删的还在。bind mount 下写进镜像即刻对沙箱
    可见，其余 provider 由各自的按需物化路径兜底。
    """
    from core.llm.tools import myspace_mirror as _mm

    try:
        await asyncio.to_thread(_mm.pull_myspace_updates, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — 正向同步失败不该拦住命令本身
        logger.warning("[bash.myspace-sync] 正向同步失败（不影响执行）: %s", exc)


async def _confirm_myspace_change(
    *,
    interactive: bool,
    chat_id: Optional[str],
    op: str,
    logical_path: str,
    summary: str,
) -> Optional[dict]:
    """要不要为这次改动征求用户同意；返回 None 表示放行。

    **问不到人就直接放行**：子智能体、批量执行、定时任务没有对话可弹确认，此时拒绝并不能
    阻止事情发生 —— 文件在沙箱里早就改了/删了，被拦住的只是"把这件事同步回用户空间"，
    结果是界面上还是旧状态、两边不一致。确认门是用来征求意见的，不是用来在无人应答时
    兜底拒绝的。
    """
    if not interactive:
        return None
    from core.llm.tools._common import myspace_write_guard

    return await myspace_write_guard(
        chat_id=chat_id,
        op=op,
        logical_path=logical_path,
        is_myspace=True,
        interactive=True,
        summary=summary,
    )


async def _snapshot_myspace(user_id: str) -> dict:
    """记下镜像目录当前的 {路径: mtime}（失败返回空字典，退化成只按时间窗口对账）。"""
    from core.llm.tools import myspace_mirror as _mm
    from core.sandbox import get_sandbox_provider as _get_provider

    if not getattr(_get_provider(), "myspace_mirror_live", False):
        return {}
    try:
        return await asyncio.to_thread(_mm.snapshot_mirror_state, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[bash.myspace-sync] 快照失败（退化为时间窗口对账）: %s", exc)
        return {}


async def _sync_myspace_changes(
    *,
    sess: Optional[str],
    user_id: str,
    chat_id: Optional[str],
    interactive: bool,
    since_ts: float,
    mirror_before: Optional[dict] = None,
) -> tuple[list[dict], list[str], list[str]]:
    """bash 执行完后，把沙箱对「我的空间」的改动登记回用户空间。

    写在 ``/myspace`` 下的文件**就是**用户的文件，两类分开处理：

    - **新文件**（还没有 artifact 记录）：直接登记、直接展示，不打断用户。子智能体、批量、
      定时任务写的也一样 —— 内容已经落在用户空间的磁盘上，此时再拦只会造出"界面看不见却
      删不掉、还被下一个会话挂到"的隐形文件。
    - **改动用户已有的文件**：主对话里过 ``MYSPACE_WRITE_CONFIRM`` 确认门，覆盖既有文件
      要用户点头；子智能体 / 批量 / 定时任务这类问不到人的场景直接生效，不拒绝 ——
      拒绝拦不住已经发生的改动，只会让两边不一致。
    - **删掉用户已有的文件**（``rm`` 不经过任何工具，靠命令前后的清单差集认出来）：同上，
      主对话问一声、非交互直接生效。被用户拒绝时不删，下一条命令的正向同步会把文件拉回
      镜像 —— 两边仍然一致。

    判定基准是 **artifact 记录**，不是镜像缓存。开了 myspace bind mount 之后沙箱的
    ``/workspace/myspace/{uid}`` 和后端 ``myspace_cache/{uid}`` 是同一份目录，旧实现
    "沙箱文件 md5 == 缓存文件 md5 就跳过"在这种拓扑下恒等为真，于是一个文件也没同步出去。

    判定用的是**执行前拍的目录快照**（``mirror_before``）与执行后的差集 —— 拿目录自己和
    自己比，不依赖任何时钟，长命令、短命令一视同仁。拿不到快照时退化成时间窗口
    （``since_ts``，内部会为内核粗粒度时钟让出余量）。用户已经删掉的文件绝不复活。

    返回 ``(已登记的 artifact ref, 被确认门拦下的路径, 已同步删除的路径)``；任何一步失败
    都只降级为告警，不影响 bash 本身的结果。
    """
    from core.llm.tools import myspace_mirror as _mm
    from core.llm.tools._common import pin_artifact_to_workspace
    from core.llm.tools._myspace_confirm import OP_DELETE, OP_WRITE
    from core.sandbox import get_sandbox_provider as _get_provider

    if not getattr(_get_provider(), "myspace_mirror_live", False):
        synced, blocked = await _sync_via_sandbox_copy(
            sess=sess,
            user_id=user_id,
            chat_id=chat_id,
            interactive=interactive,
            since_ts=since_ts,
        )
        return synced, blocked, []

    # 优先拿执行前的快照做差集：文件 mtime 取自内核粗粒度时钟，会比 time.time() 慢几毫秒，
    # 单靠时间窗口会把命令刚写出来的文件判成"窗口之前的"而漏掉（实测慢 6~10ms）。
    changes = await asyncio.to_thread(
        _mm.collect_mirror_changes,
        user_id=user_id,
        since_ts=None if mirror_before else since_ts,
        baseline=mirror_before or None,
    )
    synced: list[dict] = []
    blocked: list[str] = []
    deleted: list[str] = []

    # 新文件不需要逐个问用户，可以并发登记：登记要把内容传进对象存储，一条命令产出几十个
    # 分片时串行等于几十次往返串起来。并发度压在 8，别把存储端打爆。
    if changes.new:
        sem = asyncio.Semaphore(8)

        async def _register(entry):
            async with sem:
                return await asyncio.to_thread(
                    _mm.register_entry, user_id=user_id, chat_id=chat_id, entry=entry
                )

        synced.extend(
            ref for ref in await asyncio.gather(*(_register(e) for e in changes.new)) if ref
        )

    for entry in changes.modified:
        guard = await _confirm_myspace_change(
            interactive=interactive,
            chat_id=chat_id,
            op=OP_WRITE,
            logical_path=entry.logical_path,
            summary=f"bash 修改了 {entry.logical_path}，同步回我的空间",
        )
        if guard is not None:
            blocked.append(entry.logical_path)
            continue
        ref = await asyncio.to_thread(
            _mm.register_entry, user_id=user_id, chat_id=chat_id, entry=entry
        )
        if ref:
            synced.append(ref)

    for rel in await asyncio.to_thread(
        _mm.deleted_since, user_id=user_id, snapshot=mirror_before or {}
    ):
        logical = f"/myspace/{rel}"
        guard = await _confirm_myspace_change(
            interactive=interactive,
            chat_id=chat_id,
            op=OP_DELETE,
            logical_path=logical,
            summary=f"bash 删除了 {logical}，同步删除我的空间里的文件",
        )
        if guard is not None:
            blocked.append(logical)
            continue
        if await asyncio.to_thread(_mm.delete_registered, user_id=user_id, rel=rel):
            deleted.append(logical)

    # 只有"这条命令就产出一个文件"时才往对话产物区钉卡片 —— 那就是它的结果。批量作业一次
    # 落几十个分片，逐个钉卡会把产物区淹掉；它们在「我的空间」里照样看得见、可下载。
    if len(synced) == 1:
        pin_artifact_to_workspace(synced[0])
    if synced or blocked or deleted:
        logger.info(
            "[bash.myspace-sync] user=%s 新登记=%d 改动=%d 删除=%d 被拦=%d",
            user_id, len(changes.new), len(changes.modified), len(deleted), len(blocked),
        )
    return synced, blocked, deleted


async def _sync_via_sandbox_copy(
    *,
    sess: Optional[str],
    user_id: str,
    chat_id: Optional[str],
    interactive: bool,
    since_ts: float,
) -> tuple[list[dict], list[str]]:
    """沙箱与镜像不是同一份目录时（script_runner / cube）：把改动取回来再登记。

    这种拓扑下镜像缓存确实等于「我的空间」里的内容，所以仍用 md5 比对判断有没有改；新旧
    文件的分工与 bind mount 路径一致 —— 新文件直接登记，改用户已有文件要过确认门。
    """
    from core.config.settings import settings as _settings
    from core.llm.tools import myspace_mirror as _mm
    from core.llm.tools import myspace_vfs as _ms
    from core.llm.tools._common import (
        pin_artifact_to_workspace,
        sandbox_exec_bash,
        shell_quote,
    )
    from core.llm.tools._myspace_confirm import OP_WRITE
    from core.sandbox import SandboxConnectError as _SCE
    from core.sandbox import SandboxError as _SE
    from core.sandbox import get_sandbox_provider as _get_provider
    from core.sandbox._common import WORKSPACE as _WS

    base = f"{_WS}/myspace/{user_id}"
    max_bytes = _settings.sandbox.artifact_max_bytes
    list_cmd = (
        f"cd {shell_quote(base)} 2>/dev/null && "
        f"find . -type f -newermt @{int(since_ts)} -size -{max_bytes}c "
        f"-exec md5sum {{}} + 2>/dev/null || true"
    )
    code, out, _err = await sandbox_exec_bash(
        list_cmd, chat_id=sess, user_id=user_id, timeout=20
    )
    if code != 0 or not out.strip():
        return [], []

    candidates: list[str] = []
    for line in out.strip().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        sandbox_md5, rel = parts[0], parts[1].strip().removeprefix("./")
        if not rel:
            continue
        try:
            cache_fp = _ms.myspace_cache_file(user_id, rel)
            cache_md5 = (
                hashlib.md5(cache_fp.read_bytes()).hexdigest() if cache_fp.is_file() else None
            )
        except Exception:  # noqa: BLE001
            cache_md5 = None
        if cache_md5 != sandbox_md5:
            candidates.append(rel)
    if not candidates:
        return [], []

    states = await asyncio.to_thread(_mm.classify_rels, user_id=user_id, rels=candidates)
    provider = _get_provider()
    synced: list[dict] = []
    blocked: list[str] = []
    for rel in candidates:
        state = states.get(rel, "new")
        if state == "deleted":
            continue  # 用户删掉的文件不复活
        logical = f"/myspace/{rel}"
        if state == "modified":
            guard = await _confirm_myspace_change(
                interactive=interactive,
                chat_id=chat_id,
                op=OP_WRITE,
                logical_path=logical,
                summary=f"bash 修改了 {logical}，同步回我的空间",
            )
            if guard is not None:
                blocked.append(logical)
                continue
        try:
            # cube provider 返回 bytearray，而 OSS put_object 会把非 bytes 当文件对象处理，
            # 统一转成 bytes。
            data = bytes(await provider.get_file(sess, f"{base}/{rel}", user_id=user_id))
        except (_SE, _SCE) as exc:
            logger.warning("[bash.myspace-sync] get_file %s 失败: %s", rel, exc)
            continue
        ref = await asyncio.to_thread(
            _mm.register_bytes, user_id=user_id, chat_id=chat_id, rel=rel, content=data
        )
        if ref:
            synced.append(ref)
    if len(synced) == 1:
        pin_artifact_to_workspace(synced[0])
    return synced, blocked


def register_bash(
    toolkit: Toolkit,
    *,
    loader: Any,
    loaded_skill_ids: set[str],
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    interactive: bool = True,
) -> None:
    """Register the generic ``bash`` tool.

    Skill files — built-in and DB/admin-imported alike — are exposed read-only at
    the one path ``/workspace/skills/<id>`` (see
    ``opensandbox_provider._make_skills_volumes`` + ``config.get_sandbox_skills_dir``):
    what is bound there is the **caller's own skill view**, holding the shared
    skills plus that user's private ones, so no one sees another user's skill
    files. This registration just sets up the bash tool itself; ``loader`` /
    ``loaded_skill_ids`` are kept for backward compat with existing callers.

    The sandbox session is bound to ``chat_id`` so OpenSandbox keeps a single
    persistent container per conversation (variables, pip packages, /workspace
    files all persist between bash calls). script_runner uses the same identity
    to select a session-scoped workspace inside its sidecar.
    """
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        return

    # Effective sandbox session (``None`` → legacy fall back to chat_id).
    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def bash(command: str, timeout: int = 60) -> ToolResponse:
        from core.sandbox import ExecuteRequest as _ExecuteRequest
        from core.sandbox import SandboxConnectError as _SandboxConnectError
        from core.sandbox import SandboxError as _SandboxError
        from core.sandbox import SandboxTimeoutError as _SandboxTimeoutError
        from core.sandbox import get_sandbox_provider as _get_provider

        cmd = (command or "").strip()
        if not cmd:
            return _resp_json({"error": "command 不能为空"})

        # The generic permission middleware has already evaluated policy and
        # confirmation. The execution boundary consumes its exact command
        # ticket and applies the precomputed OS confinement constraints.
        from core.config.local_mode import local_mode_enabled

        _confinement_warning = ""
        if local_mode_enabled():
            from core.llm.tool_permissions import current_local_command_authorization

            authorization = current_local_command_authorization(cmd)
            if authorization is None:
                return _resp_json(
                    {
                        "error": ("本机命令缺少匹配的预执行授权票据，已拒绝执行"),
                        "exit_code": -1,
                        "blocked": True,
                    }
                )

            # The confinement contract belongs to the permission preset, not to
            # this call site: the authorization decides whether isolation is
            # required, preferred or waived, and this only applies the result.
            from core.llm.tool_permissions import LocalConfinementUnavailableError

            try:
                confined = authorization.confine(cmd)
            except LocalConfinementUnavailableError as exc:
                return _resp_json(
                    {
                        "error": str(exc),
                        "exit_code": -1,
                        "blocked": True,
                        "sandbox_unavailable": True,
                    }
                )
            cmd = confined.command
            if confined.warning:
                logger.warning("[local-exec] %s cmd=%r", confined.warning, cmd[:200])
                _confinement_warning = confined.warning

        provider = _get_provider()

        effective_timeout = max(1, min(int(timeout or 60), 120))
        req = _ExecuteRequest(
            script_content=cmd,
            script_name="_bash.sh",
            language="bash",
            timeout=effective_timeout,
            session_id=_sess,
            user_id=user_id,
        )
        # 先把「我的空间」的最新状态落进镜像，命令看到的才是用户当下的文件；随后记下镜像
        # 里现有哪些文件，命令跑完做差集才认得出沙箱里删掉了什么（rm 不经过任何工具）。
        mirror_before: dict = {}
        if user_id:
            await _pull_myspace_updates(user_id)
            mirror_before = await _snapshot_myspace(user_id)

        started_at = time.time()
        try:
            result = await provider.execute(req)
        except _SandboxTimeoutError as exc:
            return _resp_json({"error": str(exc), "exit_code": -1})
        except (_SandboxConnectError, _SandboxError) as exc:
            return _resp_json({"error": str(exc), "exit_code": -1})

        payload: dict = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "execution_time_ms": result.execution_time_ms,
        }
        if _confinement_warning:
            # Degraded isolation is reported, never silent: the command policy
            # gate still ran, but the OS write jail did not.
            payload["confinement_warning"] = _confinement_warning

        # 命令跑完就对账，不看命令文本、不看退出码：路径可以由变量拼出、可以先 cd 再用
        # 相对路径，靠命令里有没有 "myspace" 字样判断必然漏；命令失败前写出的文件同样已经
        # 落盘，一样要登记。真正的差异判定在 _sync_myspace_changes 里，没有变化时只有一次
        # 目录遍历。
        if user_id:
            try:
                synced, blocked, deleted = await _sync_myspace_changes(
                    sess=_sess,
                    user_id=user_id,
                    chat_id=chat_id,
                    interactive=interactive,
                    since_ts=started_at,
                    mirror_before=mirror_before,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[bash.myspace-sync] 同步异常（不影响 bash 结果）: %s", exc)
                synced, blocked, deleted = [], [], []
            if synced:
                # 批量作业一条命令可能落几十上百个文件，全塞进工具结果会把上下文撑爆：
                # 只回条数和前若干条，完整清单用户在「我的空间」里看得到。
                payload["myspace_synced_count"] = len(synced)
                payload["myspace_synced"] = synced[:10]
                payload["note"] = (
                    f"命令写入「我的空间」的 {len(synced)} 个文件已同步（同 file_id，"
                    "下载/预览链接不变），无需再调其他工具。"
                )
            if deleted:
                payload["myspace_deleted"] = deleted
                payload["note_deleted"] = (
                    "命令删除的文件已同步从「我的空间」移除：" + "、".join(deleted[:10])
                )
            if blocked:
                payload["myspace_sync_blocked"] = blocked
                payload["note_blocked"] = (
                    "以下文件的改动未获用户确认，仅保留在沙盒副本中、"
                    "未同步回我的空间：" + "、".join(blocked)
                )

        # dws (DingTalk CLI) PAT per-scope authorization interception: exit code 4
        # + a PAT_AUTHORIZATION_URL=<url> line on stderr. Surface the link to the
        # model in structured form so it hands it verbatim to the user, who
        # approves in DingTalk before retrying the original command (HITL P1 text
        # version; a proper authorization card is roadmap P2).
        pat = _detect_dws_pat_authorization(result.exit_code, result.stdout, result.stderr)
        if pat:
            payload["dingtalk_pat_authorization"] = pat
            payload["note"] = (
                "钉钉需要逐项授权（PAT）：把下面的授权链接原样发给用户，请其在钉钉中"
                "点击同意授权后，再重试刚才的 dws 命令。不要绕过授权或改用其它方式。\n"
                f"授权链接：{pat['authorization_url']}"
            )

        return _resp_json(payload)

    from core.sandbox._common import WORKSPACE as _WS

    bash.__doc__ = (
        "在沙盒里执行一条 shell 命令（默认 bash 解释器）。\n\n"
        "约定：\n"
        f"- 工作目录默认 {_WS}。已加载的技能文件位于 {_WS}/skills/<skill_id>/，\n"
        f'  典型用法：bash(command="cd {_WS}/skills/<id> && bash scripts/foo.sh")。\n'
        "- 用户「我的空间」在沙盒里就挂在 /myspace/ 下，写进去的文件会自动同步回\n"
        "  「我的空间」，不必再登记。路径只有 /myspace/... 这一种写法。\n"
        f"- 多步骤工作流可以连用多次 bash——{_WS} 在整轮对话内是持久的，\n"
        "  上一条命令写下的文件下一条命令直接能读。\n"
        "- 用户上传的文件不会自动出现在沙盒里。需要时先调 \n"
        f"  sandbox_put_artifact(artifact_id, dest_path) 把它拷进 {_WS}。\n"
        "- 脚本产出的文件如需让用户下载，调用 sandbox_get_artifact(src_path) 把它\n"
        "  登记成 artifact——bash 本身不会自动登记产物。\n\n"
        "Args:\n"
        "    command (`str`): 完整 shell 命令字符串。可以包含管道、重定向、\n"
        "        here-doc、命令链 (&&, ;, ||) 等任意 bash 语法。\n"
        "    timeout (`int`): 单次命令最大执行秒数。默认 60，硬上限 120。\n\n"
        "Returns:\n"
        "    JSON: {stdout, stderr, exit_code, execution_time_ms}\n"
        "    或失败时 {error, exit_code: -1}。\n"
    )

    toolkit.register_tool_function(bash, namesake_strategy="override")

    # Lab-mode tool family is Title-cased (``Read`` / ``Edit`` / ``Write`` /
    # ``Glob`` / ``Grep`` / ``Delete`` / ``Move`` / ``CreateFolder``). Models
    # trained on the Claude Code convention pattern-match the rest of that
    # family and call ``Bash`` (capital B) — we observed this in live runs
    # (chat_5639ac31661543c7: model emitted ``Bash`` → FunctionNotFoundError,
    # then fell back to ``excel_create_workbook`` for a PPT request). Register
    # an alias under the upper-cased name so either form resolves to the same
    # sandbox executor.
    # The alias carries a one-line description rather than a copy of ``bash``'s:
    # the full text is ~950 chars of schema that would be prefilled twice on
    # every request against a gateway without prefix caching, and repeating the
    # guidance under two names also invites the model to treat them as two
    # different tools. The name is the whole point of this registration.
    async def Bash(command: str, timeout: int = 60) -> ToolResponse:  # noqa: N802
        return await bash(command=command, timeout=timeout)

    Bash.__doc__ = (
        "Alias of `bash` — identical behaviour and arguments. Prefer `bash`.\n\n"
        "Args:\n"
        "    command (`str`): 完整 shell 命令字符串。\n"
        "    timeout (`int`): 单次命令最大执行秒数。默认 60，硬上限 120。\n"
    )
    toolkit.register_tool_function(Bash, namesake_strategy="override")
    logger.info("[factory] Registered bash tool (chat_id=%s) [alias: Bash]", chat_id)


def register_sandbox_put_artifact(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Stage an artifact (user upload or previous output) into the sandbox FS."""
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        return

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def sandbox_put_artifact(artifact_id: str, dest_path: str) -> ToolResponse:
        from core.sandbox import SandboxConnectError as _SandboxConnectError
        from core.sandbox import SandboxError as _SandboxError
        from core.sandbox import get_sandbox_provider as _get_provider

        if not artifact_id or not isinstance(artifact_id, str):
            return _resp_json({"error": "artifact_id 必须为非空字符串"})

        path_err = _validate_workspace_path(dest_path)
        if path_err:
            return _resp_json({"error": path_err})
        # Alias the canonical /workspace → real root before handing to the provider
        # (no-op in Docker); the model writes /workspace paths from the prompt/skills.
        from ._paths import canonicalize_ws_path

        dest_path = canonicalize_ws_path(dest_path)

        # _resolve_artifact_files accepts the {filename: artifact_id} shape;
        # using dest_path as the key is fine — it is only the key of the returned dict.
        files_b64, err = _resolve_artifact_files({dest_path: artifact_id}, user_id)
        if err:
            return _resp_json({"error": err})
        if not files_b64:
            return _resp_json({"error": f"artifact '{artifact_id}' 解析失败"})

        try:
            content = base64.b64decode(files_b64[dest_path])
        except Exception as exc:  # noqa: BLE001
            return _resp_json({"error": f"artifact 字节解码失败: {exc}"})

        provider = _get_provider()
        try:
            await provider.put_file(_sess, dest_path, content, user_id=user_id)
        except (_SandboxError, _SandboxConnectError) as exc:
            return _resp_json({"error": str(exc)})

        return _resp_json(
            {
                "ok": True,
                "artifact_id": artifact_id,
                "dest_path": dest_path,
                "size": len(content),
            }
        )

    sandbox_put_artifact.__doc__ = (
        "把已存在的 artifact（用户上传的、或之前产出的文件）拷贝到沙盒路径，\n"
        "供 bash/脚本读取处理。\n\n"
        "Args:\n"
        "    artifact_id (`str`): artifact 的 file_id（如 ua_xxx）。必须属于当前用户。\n"
        "    dest_path (`str`): 沙盒里的目标绝对路径，必须以 /workspace/ 开头，\n"
        "        不允许包含 .. 路径段。父目录会自动创建。\n\n"
        "Returns:\n"
        "    JSON: {ok: true, artifact_id, dest_path, size} 成功；\n"
        "    {error: '...'} 失败（artifact 不存在、无权访问、写入失败等）。\n"
        "限制：单个 artifact 最大 10 MB。\n"
    )

    toolkit.register_tool_function(sandbox_put_artifact, namesake_strategy="override")
    logger.info("[factory] Registered sandbox_put_artifact tool (chat_id=%s)", chat_id)


def register_sandbox_get_artifact(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Read a sandbox file and register it as a downloadable artifact."""
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        return

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)
    from core.config.settings import settings as _settings

    max_bytes = _settings.sandbox.artifact_max_bytes

    async def sandbox_get_artifact(src_path: str, name: str = "") -> ToolResponse:
        import mimetypes as _mt

        from core.sandbox import SandboxConnectError as _SandboxConnectError
        from core.sandbox import SandboxError as _SandboxError
        from core.sandbox import get_sandbox_provider as _get_provider

        path_err = _validate_workspace_path(src_path)
        if path_err:
            return _resp_json({"error": path_err})
        from ._paths import canonicalize_ws_path

        src_path = canonicalize_ws_path(src_path)

        provider = _get_provider()
        from core.sandbox import SandboxFileTooLargeError as _SandboxFileTooLargeError

        suffix = Path(src_path).suffix
        with tempfile.NamedTemporaryFile(
            prefix="sandbox-artifact-", suffix=suffix, delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            size = await provider.get_file_to_path(
                _sess,
                src_path,
                tmp_path,
                max_bytes=max_bytes,
                user_id=user_id,
            )
        except _SandboxFileTooLargeError as exc:
            tmp_path.unlink(missing_ok=True)
            suggestion = "PDF 请按页拆分为多个文件后逐个交付；其他格式请拆包或降低内容体积。"
            return _resp_json(
                {
                    "error": (
                        f"文件 {src_path} 过大: {exc.actual_size} bytes > " f"{exc.max_size} bytes"
                    ),
                    "code": "sandbox_artifact_too_large",
                    "actual_size": exc.actual_size,
                    "max_size": exc.max_size,
                    "suggestion": suggestion,
                }
            )
        except (_SandboxError, _SandboxConnectError) as exc:
            tmp_path.unlink(missing_ok=True)
            return _resp_json({"error": str(exc)})
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        try:
            if size <= 0:
                return _resp_json({"error": f"文件 {src_path} 为空"})

            out_name = (name or src_path.rsplit("/", 1)[-1]).strip() or "output"
            mime, _ = _mt.guess_type(out_name)
            mime = mime or "application/octet-stream"

            ref = await asyncio.to_thread(
                _store_generated_file_path,
                tmp_path,
                name=out_name,
                mime_type=mime,
                user_id=user_id,
                source="sandbox_get_artifact",
                extra_metadata={"src_path": src_path} if src_path else None,
            )
            if not ref:
                return _resp_json({"error": "artifact 登记失败（存储后端不可用？）"})

            return _resp_json(
                {
                    "ok": True,
                    "file_id": ref["file_id"],
                    "name": ref["name"],
                    "url": ref["url"],
                    "mime_type": ref["mime_type"],
                    "size": ref["size"],
                    # frontend ToolOutputRenderer expects download links rendered as an artifacts array
                    "artifacts": [ref],
                }
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    sandbox_get_artifact.__doc__ = (
        "把沙盒文件登记为持久 artifact 并返回 file_id。\n\n"
        "⚠️ **登记 ≠ 交付**：返回的 url 默认对用户隐藏，必须再调\n"
        "`pin_to_workspace(file_ids=[...])` 文件才作为附件出现在对话区；\n"
        "**禁止**把 file_id 或 url 写进正文当下载链接。\n\n"
        "Args:\n"
        "    src_path (`str`): 沙盒里的源文件绝对路径，必须以 /workspace/ 开头。\n"
        "    name (`str`, 可选): 用户面向的文件名。不传则取 src_path 的 basename。\n\n"
        "Returns:\n"
        "    JSON: {ok: true, file_id, name, url, mime_type, size, artifacts: [...]}\n"
        "    或 {error: '...'}。\n"
        f"限制：单文件最大 {max_bytes} bytes（默认 100 MiB，可由 "
        "SANDBOX_ARTIFACT_MAX_BYTES 配置）。超限时不要反复尝试同一文件；"
        "PDF 应按页拆分，其他格式应拆包或降低体积后再逐个登记。\n"
    )

    toolkit.register_tool_function(sandbox_get_artifact, namesake_strategy="override")
    logger.info("[factory] Registered sandbox_get_artifact tool (chat_id=%s)", chat_id)
