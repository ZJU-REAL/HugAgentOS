"""Sandbox-backed agent tools: ``Bash`` + artifact staging.

- ``Bash``: run a shell command inside the per-chat sandbox container.
- ``sandbox_put_artifact``: copy an existing artifact's bytes into the sandbox.
- ``sandbox_get_artifact``: read a sandbox file and register it as a
  downloadable artifact.

Relocated from the former ``core.llm.tool`` module so the singular ``tool.py``
no longer coexists with this ``tools/`` package.
"""

from __future__ import annotations
from core.llm.factory.tools.definitions import function_tool

import asyncio
import logging
import os
from typing import Any, Optional

from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolChunk).
from agentscope.tool._response import ToolChunk
from core.llm.tools._common import resolve_sandbox_session
from core.llm.tools._tool_helpers import resp_json as _resp_json


import re as _re

logger = logging.getLogger(__name__)

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


def register_bash(
    toolkit: Toolkit,
    *,
    loader: Any,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    scope: Any = None,
    source_run_id: Optional[str] = None,
) -> None:
    """Register the generic ``Bash`` tool.

    Skill files — built-in and DB/admin-imported alike — are exposed read-only at
    the one path ``/workspace/skills/<id>`` (see
    ``opensandbox_provider._make_skills_volumes`` + ``config.get_sandbox_skills_dir``):
    what is bound there is the **caller's own skill view**, holding the shared
    skills plus that user's private ones, so no one sees another user's skill
    files. This registration just sets up the Bash tool itself; ``loader`` only
    supplies the capability run that scopes it.

    The sandbox session is bound to ``chat_id`` so OpenSandbox keeps a single
    persistent container per conversation (variables, pip packages, /workspace
    files all persist between Bash calls). script_runner uses the same identity
    to select a session-scoped workspace inside its sidecar.
    """
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        return

    # Effective sandbox session (``None`` → legacy fall back to chat_id).
    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)
    from core.harness.registry.evaluation import is_evaluation_session
    _evaluation = is_evaluation_session(_sess)

    async def Bash(
        command: str, timeout: int | None = None, yield_time_ms: int = 60000,
        run_in_background: bool = False,
    ) -> ToolChunk:
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
            from core.sandbox.permissions.dispatch_models import current_local_command_authorization

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
            from core.sandbox.permissions.dispatch_models import LocalConfinementUnavailableError

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

        from core.files.edition import project_directory
        root = project_directory(scope)
        if root:
            import shlex
            cmd = f"cd {shlex.quote(root)} && " + cmd

        if timeout is not None and timeout <= 0:
            return _resp_json({"error": "timeout 必须为正数；省略表示不设置命令执行期限。"})
        from core.sandbox.workspace.resolution import workspace_directory

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
        from fastapi import HTTPException
        try:
            from core.llm.ports import command_execution
            payload = await command_execution().start(provider, req, chat_id=chat_id,
                source_run_id=source_run_id, background=run_in_background,
                yield_time_ms=yield_time_ms)
        except HTTPException as exc:
            return _resp_json({"error": exc.detail, "status": exc.status_code})
        except _SandboxTimeoutError as exc:
            return _resp_json({"error": str(exc), "exit_code": -1})
        except (_SandboxConnectError, _SandboxError) as exc:
            return _resp_json({"error": str(exc), "exit_code": -1})

        if payload.get("status") == "running":
            return _resp_json(payload)
        return await finish(payload)

    async def write_stdin(session_id: str, chars: str = "", yield_time_ms: int = 0) -> ToolChunk:
        from core.llm.ports import command_execution
        from core.llm.ports import command_records
        tid = await asyncio.to_thread(command_records().resolve_id, session_id, user_id, _sess)
        if chars not in ("", "\x03"):
            return _resp_json({"error": "stdin is closed; use empty input or Ctrl+C"})
        if chars:
            await command_execution().cancel(tid, user_id, _sess)
        else:
            await command_execution().wait(tid, user_id, _sess, max(0, min(yield_time_ms, 3000)))
        payload = await asyncio.to_thread(command_records().read, tid, user_id, _sess)
        return await finish(payload)

    async def background_process(session_id: str) -> ToolChunk:
        """Adopt an existing Bash process without executing the command again."""
        from core.sandbox import get_sandbox_provider
        from core.llm.ports import command_execution
        payload = await command_execution().adopt(get_sandbox_provider(), session_id,
            user_id=user_id, session_id=_sess, chat_id=chat_id, source_run_id=source_run_id)
        return _resp_json(payload)

    async def list_processes() -> ToolChunk:
        """List this user's command tasks in the current chat."""
        from core.llm.ports import command_records
        return _resp_json({"tasks": await asyncio.to_thread(command_records().list_owned, user_id, chat_id)})

    async def finish(payload: dict) -> ToolChunk:
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

    import core.sandbox.workspace.desktop as desktop_paths

    from core.sandbox.workspace.resolution import WORKSPACE_ROOT, path_rules

    rules = path_rules()
    instructions = (
        rules.bash_workspace_instructions(WORKSPACE_ROOT, _sess, scope=scope)
        if rules is desktop_paths else rules.bash_workspace_instructions(WORKSPACE_ROOT, _sess)
    )
    Bash.__doc__ = instructions + (
        "Args:\n"
        "    command (`str`): 完整 shell 命令字符串。可以包含管道、重定向、\n"
        "        here-doc、命令链 (&&, ;, ||) 等任意 Bash 语法。\n"
        "    yield_time_ms (`int`): 首次等待毫秒数，默认 60000，范围 250–60000。等待到期不会杀进程。\n"
        "    run_in_background (`bool`): 立即转后台，默认 false；前台等待到期仍运行也自动转后台。\n"
        "    timeout (`int`, 可选): 显式命令执行期限（秒）。默认不设置；不再有 120 秒硬上限。\n\n"
        "Returns:\n"
        "    JSON: {stdout, stderr, exit_code, execution_time_ms, status, session_id}。\n"
        "    status=running 时已交给后台管理；完成后自动通知。继续独立工作，不要轮询或重启命令。没有独立工作时结束本次回复，等待完成事件唤醒。\n"
        "    只有 exited 且 exit_code=0 才表示执行成功。当前为非交互执行，stdin 关闭，不提供 PTY。\n"
        "    或失败时 {error, exit_code: -1}。\n"
    )

    from core.files.edition import command_instructions
    Bash.__doc__ += command_instructions(scope)

    write_stdin.__doc__ = (
        "按需读取 Bash 新增输出、短时等待或取消；不会重新执行命令。后台完成会自动通知，不要循环调用本工具或用 sleep 等待。\n\n"
        "Args:\n"
        "    session_id (`str`): Bash 返回的进程会话 ID，不是对话 ID。\n"
        "    chars (`str`): 空字符串表示等待；\\u0003 表示 Ctrl+C 中断。非 PTY 模式不接受其它输入。\n"
        "    yield_time_ms (`int`): 按需短读的等待毫秒数，默认 0，最多 3000。完成通知由后端负责，不要主动等待或循环读取。\n\n"
        "Returns:\n"
        "    JSON: {stdout, stderr, exit_code, execution_time_ms, status, session_id}；输出仅含新增内容。\n"
        "    running 表示继续运行；exited 时检查 exit_code 和 error。\n"
    )
    toolkit.append(function_tool(background_process))
    toolkit.append(function_tool(list_processes))
    toolkit.append(function_tool(write_stdin))
    toolkit.append(function_tool(Bash))

    logger.info("[factory] Registered Bash tool (chat_id=%s)", chat_id)



from .sandbox_artifact_tools import register_sandbox_get_artifact, register_sandbox_put_artifact
