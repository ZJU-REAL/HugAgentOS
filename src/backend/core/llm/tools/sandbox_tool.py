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
import logging
import os
from typing import Any, Optional

from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse
from core.llm.tools._common import resolve_sandbox_session
from core.llm.tools._tool_helpers import _resp_json

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
    from core.myspace import mirror as _mm

    try:
        await asyncio.to_thread(_mm.pull_myspace_updates, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — 正向同步失败不该拦住命令本身
        logger.warning("[bash.myspace-sync] 正向同步失败（不影响执行）: %s", exc)


def register_bash(
    toolkit: Toolkit,
    *,
    loader: Any,
    loaded_skill_ids: set[str],
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    scope: Any = None,
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
    from core.llm.evaluation_runtime import is_evaluation_session
    _evaluation = is_evaluation_session(_sess)

    async def bash(
        command: str, timeout: int | None = None, yield_time_ms: int = 60000,
    ) -> ToolResponse:
        from core.sandbox import ProcessRequest as _ProcessRequest
        from core.sandbox import SandboxConnectError as _SandboxConnectError
        from core.sandbox import SandboxError as _SandboxError
        from core.sandbox import SandboxTimeoutError as _SandboxTimeoutError
        from core.sandbox import get_sandbox_provider as _get_provider

        from .project_source_access import current_scope_error
        scope_error = current_scope_error(scope, user_id)
        if scope_error:
            return _resp_json(scope_error)
        cmd = (command or "").strip()
        if not cmd:
            return _resp_json({"error": "command 不能为空"})

        # The generic permission middleware has already evaluated policy and
        # confirmation. The execution boundary consumes its exact command ticket
        # and asks it for the OS-level launch, which the provider applies at the
        # point it actually spawns the process.
        from core.config.local_mode import local_mode_enabled

        authorization = None
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

        provider = _get_provider()

        sandbox_launch = None
        # An OS sandbox is only meaningful where the command becomes a plain host
        # process. A container provider isolates the command itself, and asking
        # it to apply an argv prefix it does not understand would produce a
        # confinement nobody enforces — so the question is put to the provider
        # rather than assumed from the deployment profile.
        if authorization is not None and provider.runs_on_host:
            from core.llm.tool_permissions import LocalConfinementUnavailableError

            try:
                sandbox_launch = authorization.confine()
            except LocalConfinementUnavailableError as exc:
                return _resp_json(
                    {
                        "error": str(exc),
                        "exit_code": -1,
                        "blocked": True,
                        "sandbox_unavailable": True,
                    }
                )
            logger.info(
                "[local-exec] preset=%s sandbox=%s",
                authorization.approval_mode,
                sandbox_launch.backend if sandbox_launch else "未启用（用户选择的权限档）",
            )

        from core.services.edition_workspace import project_directory
        root = project_directory(scope)
        if root:
            import shlex
            cmd = f"cd {shlex.quote(root)} && " + cmd

        if timeout is not None and timeout <= 0:
            return _resp_json({"error": "timeout 必须为正数；省略表示不设置命令执行期限。"})
        from ._paths import workspace_directory

        effective_timeout = timeout
        req = _ProcessRequest(
            script_content=cmd,
            script_name="_bash.sh",
            capability_run_id=getattr(getattr(loader, "capability_run", None), "run_id", None),
            capability_scope=getattr(getattr(loader, "capability_run", None), "scope_id", ""),
            language="bash",
            timeout=effective_timeout,
            session_id=_sess,
            user_id=user_id,
            sandbox_launch=sandbox_launch,
            cwd=(
                workspace_directory(_sess, scope=scope)
                if local_mode_enabled() and not _evaluation else None
            ),
        )
        # 把「我的空间」的最新状态落进镜像，命令看到的才是用户当下的文件。反方向
        # （命令写了什么、删了什么）不在这里判断：那由 core.myspace.watcher 从文件
        # 事件登记，命令返回之后才落盘的后台进程也一样收得到。
        if user_id and not _evaluation:
            await _pull_myspace_updates(user_id)

        from fastapi import HTTPException
        try:
            payload = await provider.start_process(req, yield_time_ms=yield_time_ms)
        except HTTPException as exc:
            return _resp_json({"error": exc.detail, "status": exc.status_code})
        except _SandboxTimeoutError as exc:
            return _resp_json({"error": str(exc), "exit_code": -1})
        except (_SandboxConnectError, _SandboxError) as exc:
            return _resp_json({"error": str(exc), "exit_code": -1})

        if payload.get("status") == "running":
            return _resp_json(payload)
        return await finish(payload)

    async def write_stdin(session_id: str, chars: str = "", yield_time_ms: int = 60000) -> ToolResponse:
        from core.sandbox import get_sandbox_provider
        from fastapi import HTTPException
        try:
            payload = await get_sandbox_provider().write_stdin(
                session_id, sandbox_session_id=_sess, user_id=user_id,
                chars=chars, yield_time_ms=yield_time_ms,
            )
        except HTTPException as exc:
            return _resp_json({"error": exc.detail, "status": exc.status_code})
        if payload.get("status") == "running":
            return _resp_json(payload)
        return await finish(payload)

    async def finish(payload: dict) -> ToolResponse:
        from core.config.settings import settings
        if user_id and not _evaluation and settings.sandbox.provider == "opensandbox":
            from core.services.edition_workspace import persist_user
            try:
                payload["workspace_synced_count"] = await asyncio.to_thread(persist_user, user_id)
            except Exception as exc:
                payload["error"] = getattr(exc, "detail", str(exc))
                payload["source_saved"] = False

        # 沙箱的 /myspace 不在本机时（script_runner / cube），把它的现状搬进镜像目录，
        # 之后的判定与登记由 core.myspace.watcher 按同一套判据完成。bind mount 下这里
        # 直接返回 —— 沙箱写的就是镜像目录本身。
        if user_id and not _evaluation:
            from core.myspace.sandbox_sync import reflect_sandbox_myspace

            await reflect_sandbox_myspace(session_id=_sess, user_id=user_id)

        # dws (DingTalk CLI) PAT per-scope authorization interception: exit code 4
        # + a PAT_AUTHORIZATION_URL=<url> line on stderr. Surface the link to the
        # model in structured form so it hands it verbatim to the user, who
        # approves in DingTalk before retrying the original command (HITL P1 text
        # version; a proper authorization card is roadmap P2).
        pat = _detect_dws_pat_authorization(payload.get("exit_code"), payload.get("stdout", ""), payload.get("stderr", ""))
        if pat:
            payload["dingtalk_pat_authorization"] = pat
            payload["note"] = (
                "钉钉需要逐项授权（PAT）：把下面的授权链接原样发给用户，请其在钉钉中"
                "点击同意授权后，再重试刚才的 dws 命令。不要绕过授权或改用其它方式。\n"
                f"授权链接：{pat['authorization_url']}"
            )

        return _resp_json(payload)

    from ._paths import WORKSPACE_ROOT, path_rules

    from core.sandbox import desktop_paths

    rules = path_rules()
    instructions = (
        rules.bash_workspace_instructions(WORKSPACE_ROOT, _sess, scope=scope)
        if rules is desktop_paths else rules.bash_workspace_instructions(WORKSPACE_ROOT, _sess)
    )
    bash.__doc__ = instructions + (
        "Args:\n"
        "    command (`str`): 完整 shell 命令字符串。可以包含管道、重定向、\n"
        "        here-doc、命令链 (&&, ;, ||) 等任意 bash 语法。\n"
        "    yield_time_ms (`int`): 首次等待毫秒数，默认 60000，范围 250–60000。等待到期不会杀进程。\n"
        "    timeout (`int`, 可选): 显式命令执行期限（秒）。默认不设置；不再有 120 秒硬上限。\n\n"
        "Returns:\n"
        "    JSON: {stdout, stderr, exit_code, execution_time_ms, status, session_id}。\n"
        "    status=running 时使用 write_stdin(session_id, chars='') 等待同一命令；不要重新启动或用 nohup/tail 轮询。\n"
        "    只有 exited 且 exit_code=0 才表示执行成功。当前为非交互执行，stdin 关闭，不提供 PTY。\n"
        "    或失败时 {error, exit_code: -1}。\n"
    )

    from core.services.edition_workspace import command_instructions
    bash.__doc__ += command_instructions(scope)

    write_stdin.__doc__ = (
        "继续等待 bash 返回的进程会话，读取新增输出；不会重新执行命令。\n\n"
        "Args:\n"
        "    session_id (`str`): bash 返回的进程会话 ID，不是对话 ID。\n"
        "    chars (`str`): 空字符串表示等待；\\u0003 表示 Ctrl+C 中断。非 PTY 模式不接受其它输入。\n"
        "    yield_time_ms (`int`): 最多等待毫秒数，默认 60000，上限 300000。等待到期不杀进程。\n\n"
        "Returns:\n"
        "    JSON: {stdout, stderr, exit_code, execution_time_ms, status, session_id}；输出仅含新增内容。\n"
        "    running 表示继续运行；exited 时检查 exit_code 和 error。\n"
    )
    toolkit.register_tool_function(write_stdin, namesake_strategy="override")
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
    async def Bash(
        command: str, timeout: int | None = None, yield_time_ms: int = 60000,
    ) -> ToolResponse:  # noqa: N802
        return await bash(command=command, timeout=timeout, yield_time_ms=yield_time_ms)

    Bash.__doc__ = (
        "Alias of `bash` — identical behaviour and arguments. Prefer `bash`.\n\n"
        "Args:\n"
        "    command (`str`): 完整 shell 命令字符串。\n"
        "    timeout (`int`, 可选): 命令执行期限秒数，默认不设。\n"
        "    yield_time_ms (`int`): 首次等待毫秒数，默认 60000。\n"
    )
    toolkit.register_tool_function(Bash, namesake_strategy="override")
    logger.info("[factory] Registered bash tool (chat_id=%s) [alias: Bash]", chat_id)


from .sandbox_artifact_tools import register_sandbox_put_artifact, register_sandbox_get_artifact
