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
import logging
import os
import tempfile
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

    async def bash(command: str, timeout: int = 60) -> ToolResponse:
        from core.sandbox import ExecuteRequest as _ExecuteRequest
        from core.sandbox import SandboxConnectError as _SandboxConnectError
        from core.sandbox import SandboxError as _SandboxError
        from core.sandbox import SandboxTimeoutError as _SandboxTimeoutError
        from core.sandbox import get_sandbox_provider as _get_provider

        from .project_source_access import current_scope_error
        scope_error = current_scope_error(scope, user_id, write=True)
        if scope_error:
            return _resp_json(scope_error)
        team_project = scope is not None and scope.kind == "team"
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

        team_before = []
        if team_project:
            from .project_working_copy import prepare, directory
            from fastapi import HTTPException
            import shlex
            try:
                team_before = await prepare(provider, _sess, scope, user_id or "")
            except HTTPException as exc:
                return _resp_json({"error": exc.detail, "status": exc.status_code})
            root = directory(scope.project_id)
            cmd = f"mkdir -p {shlex.quote(root)} && cd {shlex.quote(root)} && " + cmd

        effective_timeout = max(1, min(int(timeout or 60), 120))
        req = _ExecuteRequest(
            script_content=cmd,
            script_name="_bash.sh",
            capability_run_id=getattr(getattr(loader, "capability_run", None), "run_id", None),
            capability_scope=getattr(getattr(loader, "capability_run", None), "scope_id", ""),
            language="bash",
            timeout=effective_timeout,
            session_id=_sess,
            user_id=user_id,
            sandbox_launch=sandbox_launch,
        )
        # 把「我的空间」的最新状态落进镜像，命令看到的才是用户当下的文件。反方向
        # （命令写了什么、删了什么）不在这里判断：那由 core.myspace.watcher 从文件
        # 事件登记，命令返回之后才落盘的后台进程也一样收得到。
        if user_id and not team_project:
            await _pull_myspace_updates(user_id)

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
        if team_project:
            from .project_working_copy import persist, directory
            try:
                payload["project_synced_count"] = await persist(_sess, scope, user_id or "", team_before)
                payload["project_directory"] = directory(scope.project_id)
                payload["note"] = "修改已同步到团队项目；删除文件请使用项目文件管理。"
            except Exception as exc:
                payload["error"] = getattr(exc, "detail", str(exc))
                payload["source_saved"] = False

        # 沙箱的 /myspace 不在本机时（script_runner / cube），把它的现状搬进镜像目录，
        # 之后的判定与登记由 core.myspace.watcher 按同一套判据完成。bind mount 下这里
        # 直接返回 —— 沙箱写的就是镜像目录本身。
        if user_id and not team_project:
            from core.myspace.sandbox_sync import reflect_sandbox_myspace

            await reflect_sandbox_myspace(session_id=_sess, user_id=user_id)

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

    from ._paths import WORKSPACE_ROOT, path_rules

    bash.__doc__ = path_rules().bash_workspace_instructions(WORKSPACE_ROOT, _sess) + (
        "Args:\n"
        "    command (`str`): 完整 shell 命令字符串。可以包含管道、重定向、\n"
        "        here-doc、命令链 (&&, ;, ||) 等任意 bash 语法。\n"
        "    timeout (`int`): 单次命令最大执行秒数。默认 60，硬上限 120。\n\n"
        "Returns:\n"
        "    JSON: {stdout, stderr, exit_code, execution_time_ms}\n"
        "    或失败时 {error, exit_code: -1}。\n"
    )

    if scope and scope.kind == "team":
        from .project_working_copy import directory
        bash.__doc__ += (
            "\n当前为团队项目。bash 自动在 " + directory(scope.project_id)
            + " 中执行并同步源码；使用相对路径或该项目目录，"
            "不要使用 /myspace 个人镜像路径。构建产物应写入 /workspace/.site-dist/。"
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

        from ._paths import to_physical_path, workspace_directory

        dest_path = to_physical_path(dest_path, user_id, session_id=_sess)
        path_err = _validate_workspace_path(dest_path, root=workspace_directory(_sess))
        if path_err:
            return _resp_json({"error": path_err})

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
        "    dest_path (`str`): 当前工作目录内的绝对路径或相对路径，\n"
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
    scope: Optional["ProjectScope"] = None,
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

        from core.config.local_mode import local_mode_enabled
        from ._paths import to_physical_path

        if local_mode_enabled():
            from core.artifacts.local_project import reference_project_file, is_project_file_path
            from fastapi import HTTPException

            local_scope = scope
            if local_scope and local_scope.is_local:
                physical = to_physical_path(src_path, user_id, session_id=_sess)
                # Project files are already durable. Only scratch exports need a copy.
                if is_project_file_path(src_path, local_scope) or is_project_file_path(physical, local_scope):
                    try:
                        ref = await asyncio.to_thread(
                            reference_project_file, physical, scope=local_scope,
                            user_id=user_id or "", name=name,
                        )
                    except (HTTPException, OSError, ValueError) as exc:
                        return _resp_json({"error": str(getattr(exc, "detail", exc))})
                    ref = {k: ref[k] for k in ("file_id", "name", "mime_type", "size")}
                    ref["url"] = f"/files/{ref['file_id']}"
                    return _resp_json({"ok": True, **ref, "artifacts": [ref]})

        from ._paths import to_physical_path, workspace_directory

        src_path = to_physical_path(src_path, user_id, session_id=_sess)
        path_err = _validate_workspace_path(src_path, root=workspace_directory(_sess))
        if path_err:
            return _resp_json({"error": path_err})

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

            out_name = (name or Path(src_path).name).strip() or "output"
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
        "登记文件并返回 file_id。本机项目文件只引用原文件，不复制到 artifacts；临时沙盒文件才导出保存。\n\n"
        "⚠️ **登记 ≠ 交付**：返回的 url 默认对用户隐藏，必须再调\n"
        "`pin_to_workspace(file_ids=[...])` 文件才作为附件出现在对话区；\n"
        "**禁止**把 file_id 或 url 写进正文当下载链接。\n\n"
        "Args:\n"
        "    src_path (`str`): 工作目录内的绝对或相对路径，或当前本机项目中的真实绝对路径。\n"
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
