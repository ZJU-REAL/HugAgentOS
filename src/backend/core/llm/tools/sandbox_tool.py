"""Sandbox-backed agent tools: ``bash`` + artifact staging.

- ``bash``: run a shell command inside the per-chat sandbox container.
- ``sandbox_put_artifact``: copy an existing artifact's bytes into the sandbox.
- ``sandbox_get_artifact``: read a sandbox file and register it as a
  downloadable artifact.

Relocated from the former ``core.llm.tool`` module so the singular ``tool.py``
no longer coexists with this ``tools/`` package.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Any, Optional

from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse

from core.llm.tools._common import resolve_sandbox_session
from core.llm.tools._tool_helpers import (
    MAX_ARTIFACT_FILE_SIZE,
    _resolve_artifact_files,
    _resp_json,
    _store_generated_files,
    _validate_workspace_path,
)

logger = logging.getLogger(__name__)

import re as _re

# dws exit code 4 = PAT authorization interception; stderr/stdout carries a line
# ``PAT_AUTHORIZATION_URL=<url>`` (a copy-safe link dws prints separately for
# OpenClaw-style hosts, see dws CHANGELOG #242).
_PAT_URL_RE = _re.compile(r"PAT_AUTHORIZATION_URL=(\S+)")


def _detect_dws_pat_authorization(
    exit_code: int, stdout: str, stderr: str
) -> Optional[dict]:
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


async def _sync_myspace_changes(
    *,
    sess: Optional[str],
    user_id: str,
    chat_id: Optional[str],
    interactive: bool,
) -> tuple[list[dict], list[str]]:
    """After bash runs, reverse-sync modified myspace files in the sandbox back to My Space.

    Background: for binary documents (docx etc.), Edit/Write steer the model toward
    "use bash to call python-docx, modify, and write back to the same /myspace
    path", but the sandbox filesystem has no write-back path to artifact storage
    (the bind-mount only shares the seeding cache) — after bash finishes, the
    sandbox copy has changed while the user's file in "My Space" is untouched,
    yet the model reports "done". This function closes that loop:

    1. List files under the sandbox ``/workspace/myspace/{uid}`` modified recently
       (within 10min) with their md5;
    2. Compare against the backend mirror cache (myspace_cache, maintained in sync
       with artifact content); skip files whose md5 matches;
    3. Each differing file passes the §13 confirmation gate (same gate as
       Write/Edit: rejected outright in non-interactive mode, suspended awaiting
       user approval in interactive mode); once approved, ``sync_upsert`` writes
       back in place (same file_id, download/preview links unchanged) and pins to
       the workspace.

    Returns ``(synced_refs, blocked_paths)``; any step failure only degrades to a
    warning and never affects the bash result itself.
    """
    from core.llm.tools import myspace_vfs as _ms
    from core.llm.tools._common import (
        myspace_write_guard,
        pin_artifact_to_workspace,
        sandbox_exec_bash,
        shell_quote,
    )
    from core.llm.tools._myspace_confirm import OP_WRITE
    from core.sandbox import (
        SandboxConnectError as _SCE,
        SandboxError as _SE,
        get_sandbox_provider as _get_provider,
    )

    from core.sandbox._common import WORKSPACE as _WS

    base = f"{_WS}/myspace/{user_id}"
    # -mmin -10: only look at recent changes, so sandbox copies left over from
    # earlier turns are not mistaken for this run's modifications (prevents old
    # files the user already deleted in the UI from being "resurrected").
    list_cmd = (
        f"cd {shell_quote(base)} 2>/dev/null && "
        f"find . -type f -mmin -10 -size -10M -exec md5sum {{}} + 2>/dev/null"
        f" || true"
    )
    code, out, _err = await sandbox_exec_bash(list_cmd, chat_id=sess, timeout=20)
    if code != 0 or not out.strip():
        return [], []

    synced: list[dict] = []
    blocked: list[str] = []
    provider = _get_provider()
    for line in out.strip().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        sandbox_md5, rel = parts[0], parts[1].strip().removeprefix("./")
        if not rel:
            continue
        # Compare against the mirror cache: the cache is maintained in sync with
        # artifact content (both materialize and sync_upsert mirror), so an equal
        # md5 = the user space already holds this content.
        try:
            cache_fp = _ms.myspace_cache_file(user_id, rel)
            cache_md5 = (
                hashlib.md5(cache_fp.read_bytes()).hexdigest()
                if cache_fp.is_file() else None
            )
        except Exception:  # noqa: BLE001
            cache_md5 = None
        if cache_md5 == sandbox_md5:
            continue

        logical = f"/myspace/{rel}"
        guard = await myspace_write_guard(
            chat_id=chat_id, op=OP_WRITE, logical_path=logical,
            is_myspace=True, interactive=interactive,
            summary=f"bash 修改了 {logical}，同步回我的空间",
        )
        if guard is not None:
            blocked.append(logical)
            continue
        try:
            # The cube provider returns bytearray, and OSS put_object treats
            # non-bytes as a file-like object (requiring .read) — normalize to bytes.
            data = bytes(await provider.get_file(sess, f"{base}/{rel}", user_id=user_id))
        except (_SE, _SCE) as exc:
            logger.warning("[bash.myspace-sync] get_file %s 失败: %s", rel, exc)
            continue
        ref = _ms.sync_upsert(
            user_id=user_id, chat_id=chat_id,
            logical_path=logical, content=data,
        )
        if ref:
            pin_artifact_to_workspace(ref)
            synced.append(ref)
            logger.info(
                "[bash.myspace-sync] %s → artifact %s (%dB, in_place=%s)",
                logical, ref.get("file_id"), len(data),
                ref.get("in_place_update"),
            )
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

    ALL skill files — built-in and DB/admin-imported — are exposed via a single
    read-only host bind mount at ``/workspace/skills/<id>`` (see
    ``opensandbox_provider._make_skills_volume`` + ``config.get_sandbox_skills_dir``):
    built-in skills are copied into the unified host dir at startup and DB skills
    are materialized into it on demand, so there's one in-sandbox path for every
    skill. This registration just sets up the bash tool itself; ``loader`` /
    ``loaded_skill_ids`` are kept for backward compat with existing callers.

    The sandbox session is bound to ``chat_id`` so OpenSandbox keeps a single
    persistent container per conversation (variables, pip packages, /workspace
    files all persist between bash calls). script_runner provider ignores
    ``session_id`` since its sidecar's /workspace is globally durable.
    """
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        return

    # Effective sandbox session (``None`` → legacy fall back to chat_id).
    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def bash(command: str, timeout: int = 60) -> ToolResponse:
        from core.sandbox import (
            ExecuteRequest as _ExecuteRequest,
            SandboxConnectError as _SandboxConnectError,
            SandboxError as _SandboxError,
            SandboxTimeoutError as _SandboxTimeoutError,
            get_sandbox_provider as _get_provider,
        )

        cmd = (command or "").strip()
        if not cmd:
            return _resp_json({"error": "command 不能为空"})

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

        # Command succeeded and touches a myspace path → reverse-sync the sandbox
        # changes back to My Space (cheap gate: check the command string first; the
        # real diff detection is in _sync_myspace_changes).
        if result.exit_code == 0 and user_id and "myspace" in cmd:
            try:
                synced, blocked = await _sync_myspace_changes(
                    sess=_sess, user_id=user_id, chat_id=chat_id,
                    interactive=interactive,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[bash.myspace-sync] 同步异常（不影响 bash 结果）: %s", exc)
                synced, blocked = [], []
            if synced:
                payload["myspace_synced"] = synced
                payload["note"] = (
                    "检测到命令修改了「我的空间」文件，已自动同步回用户空间"
                    "（同 file_id，下载/预览链接不变），无需再调其他工具。"
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
        f"  典型用法：bash(command=\"cd {_WS}/skills/<id> && bash scripts/foo.sh\")。\n"
        f"- 多步骤工作流可以连用多次 bash——{_WS} 在整轮对话内是持久的，\n"
        "  上一条命令写下的文件下一条命令直接能读。\n"
        "- 用户上传的文件不会自动出现在沙盒里。需要时先调 \n"
        f"  sandbox_put_artifact(artifact_id, dest_path) 把它拷进 {_WS}。\n"
        "- 脚本产出的文件如需让用户下载，调用 sandbox_get_artifact(src_path) 把它\n"
        "  登记成 artifact——bash 本身不会自动登记产物。\n"
        f"- **例外：「我的空间」文件**。命令修改了 {_WS}/myspace/<uid>/ 下的\n"
        "  文件（如用 python-docx 改 docx）会在命令成功后自动同步回用户「我的\n"
        "  空间」（同 file_id、链接不变，需用户确认），看返回的 myspace_synced\n"
        "  字段确认即可，不要再调 sandbox_get_artifact 重复登记。\n\n"
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
    async def Bash(command: str, timeout: int = 60) -> ToolResponse:  # noqa: N802
        return await bash(command=command, timeout=timeout)

    Bash.__doc__ = bash.__doc__
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
        from core.sandbox import (
            SandboxConnectError as _SandboxConnectError,
            SandboxError as _SandboxError,
            get_sandbox_provider as _get_provider,
        )

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

        return _resp_json({
            "ok": True,
            "artifact_id": artifact_id,
            "dest_path": dest_path,
            "size": len(content),
        })

    sandbox_put_artifact.__doc__ = (
        "把已存在的 artifact（用户上传文件，或之前 bash/工具产出的文件）的字节\n"
        "拷贝到沙盒指定路径。\n\n"
        "典型流程：\n"
        "1. 用户上传了 data.csv → 拿到 file_id 'ua_abc123'\n"
        "2. sandbox_put_artifact(artifact_id='ua_abc123', dest_path='/workspace/in.csv')\n"
        "3. bash(command='cd /workspace && python analyze.py /workspace/in.csv ...')\n\n"
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

    async def sandbox_get_artifact(src_path: str, name: str = "") -> ToolResponse:
        import mimetypes as _mt
        from core.sandbox import (
            SandboxConnectError as _SandboxConnectError,
            SandboxError as _SandboxError,
            get_sandbox_provider as _get_provider,
        )

        path_err = _validate_workspace_path(src_path)
        if path_err:
            return _resp_json({"error": path_err})
        from ._paths import canonicalize_ws_path
        src_path = canonicalize_ws_path(src_path)

        provider = _get_provider()
        try:
            content = await provider.get_file(_sess, src_path, user_id=user_id)
        except (_SandboxError, _SandboxConnectError) as exc:
            return _resp_json({"error": str(exc)})

        if not content:
            return _resp_json({"error": f"文件 {src_path} 为空"})
        if len(content) > MAX_ARTIFACT_FILE_SIZE:
            return _resp_json({
                "error": (
                    f"文件 {src_path} 过大: {len(content)} bytes > "
                    f"{MAX_ARTIFACT_FILE_SIZE} bytes"
                ),
            })

        out_name = (name or src_path.rsplit("/", 1)[-1]).strip() or "output"
        mime, _ = _mt.guess_type(out_name)
        mime = mime or "application/octet-stream"

        refs = _store_generated_files(
            [{
                "name": out_name,
                "size": len(content),
                "content_b64": base64.b64encode(content).decode("ascii"),
                "mime_type": mime,
            }],
            user_id=user_id,
            source="sandbox_get_artifact",
            extra_metadata={"src_path": src_path} if src_path else None,
        )
        if not refs:
            return _resp_json({"error": "artifact 登记失败（存储后端不可用？）"})

        ref = refs[0]
        return _resp_json({
            "ok": True,
            "file_id": ref["file_id"],
            "name": ref["name"],
            "url": ref["url"],
            "mime_type": ref["mime_type"],
            "size": ref["size"],
            # frontend ToolOutputRenderer expects download links rendered as an artifacts array
            "artifacts": [ref],
        })

    sandbox_get_artifact.__doc__ = (
        "把沙盒里的某个文件读出来，登记为持久 artifact，并返回 file_id。\n\n"
        "⚠️ **拿到 file_id ≠ 已交付**。本工具只是把沙盒文件登记进 artifact 存储，\n"
        "返回的 url（如 /files/xxx）默认对用户隐藏——直到你再调一次\n"
        "`pin_to_workspace(file_ids=[\"<file_id>\"])`，文件才会作为附件出现在对话区。\n"
        "**禁止**把 file_id 或 url 直接写进正文当下载链接，那对用户不可见。\n\n"
        "典型流程：bash 跑完生成脚本，脚本把结果写到 /workspace/out.xlsx：\n"
        "  1) sandbox_get_artifact(src_path='/workspace/out.xlsx') → 得到 file_id\n"
        "  2) pin_to_workspace(file_ids=['<file_id>']) → 文件交付给用户\n"
        "多份产物一次性 pin（同一 file_ids 列表里塞所有 id），不要分次调。\n\n"
        "Args:\n"
        "    src_path (`str`): 沙盒里的源文件绝对路径，必须以 /workspace/ 开头。\n"
        "    name (`str`, 可选): 用户面向的文件名。不传则取 src_path 的 basename。\n\n"
        "Returns:\n"
        "    JSON: {ok: true, file_id, name, url, mime_type, size, artifacts: [...]}\n"
        "    或 {error: '...'}。\n"
        "限制：单文件最大 10 MB。\n"
    )

    toolkit.register_tool_function(sandbox_get_artifact, namesake_strategy="override")
    logger.info("[factory] Registered sandbox_get_artifact tool (chat_id=%s)", chat_id)
