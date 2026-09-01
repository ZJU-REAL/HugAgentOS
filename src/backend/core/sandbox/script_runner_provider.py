"""ScriptRunnerProvider — wraps HTTP calls to the existing hugagent-script-runner container.

Behavior:
- 2 ReadTimeout retries
- ``http_timeout = req.timeout + 30`` as the outer HTTP timeout
- HTTPStatusError / ConnectError / TimeoutException classified and mapped to SandboxError subclasses
"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import httpx
from core.config.settings import settings

from ._common import stream_to_file
from .errors import SandboxConnectError, SandboxError, SandboxFileTooLargeError, SandboxTimeoutError
from .protocol import (
    ExecuteRequest,
    ExecuteResult,
    SandboxAdminCapabilities,
    SandboxAdminNotSupported,
    SandboxFile,
    SandboxInfo,
    StagedFile,
    StageFile,
)

logger = logging.getLogger(__name__)


def _connect_error_message() -> str:
    """按部署形态给出准确的连接失败提示。

    本机（DEPLOY_PROFILE=local）下 runner 是子进程而非容器——提示"检查容器"
    纯属误导；看门狗会自动重拉，提示等待/重启客户端即可。
    """
    if settings.deploy.is_local:
        return (
            "本机代码执行服务不可达（127.0.0.1:8900），已尝试自动恢复；"
            "稍候重试，若持续失败请重启客户端"
        )
    return "无法连接脚本执行服务 (hugagent-script-runner)，请检查容器是否运行"


class ScriptRunnerProvider:
    name = "script_runner"

    def __init__(self) -> None:
        # Access settings.sandbox.runner_url on each call so test monkeypatching stays effective
        pass

    @property
    def _base_url(self) -> str:
        return settings.sandbox.runner_url

    async def execute(self, req: ExecuteRequest) -> ExecuteResult:
        _refresh_skill_view(req.user_id)
        # 30s margin covers the sidecar's own overhead (base64 encoding, transfer, etc.)
        http_timeout = req.timeout + 30
        body = {
            "script_content": req.script_content,
            "script_name": req.script_name,
            "language": req.language,
            "params": req.params,
            "timeout": req.timeout,
            "resource_files": req.resource_files,
            "input_files": req.input_files,
            "input_files_b64": req.input_files_b64,
            "session_id": req.session_id,
            "user_id": req.user_id,
        }

        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            for attempt in range(2):
                try:
                    resp = await client.post(f"{self._base_url}/execute", json=body)
                    resp.raise_for_status()
                    payload = resp.json()
                    return _payload_to_result(payload)
                except httpx.ReadTimeout as e:
                    last_exc = e
                    logger.warning(
                        "[script_runner] ReadTimeout script=%s attempt=%d/2 (http_timeout=%ds)",
                        req.script_name,
                        attempt + 1,
                        http_timeout,
                    )
                    continue
                except httpx.TimeoutException as e:
                    raise SandboxTimeoutError(
                        f"脚本执行超时（{req.timeout}秒, {type(e).__name__}）"
                    ) from e
                except httpx.HTTPStatusError as e:
                    text = e.response.text if e.response is not None else str(e)
                    raise SandboxError(f"脚本执行失败: {text}") from e
                except httpx.ConnectError as e:
                    raise SandboxConnectError(_connect_error_message()) from e

        raise SandboxTimeoutError(
            f"脚本执行读取超时（{http_timeout}秒，已重试）: "
            f"{type(last_exc).__name__ if last_exc else 'ReadTimeout'}"
        )

    async def stage_files(self, user_id: str, files: list[StageFile]) -> list[StagedFile]:
        body = {
            "user_id": user_id,
            "files": [{"name": f.name, "content_b64": f.content_b64} for f in files],
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{self._base_url}/stage", json=body)
                resp.raise_for_status()
                staged_raw = resp.json().get("staged", [])
                return [StagedFile(name=item["name"], path=item["path"]) for item in staged_raw]
        except httpx.ConnectError as e:
            raise SandboxConnectError(_connect_error_message()) from e
        except httpx.HTTPStatusError as e:
            text = e.response.text if e.response is not None else str(e)
            raise SandboxError(f"暂存文件失败: {text}") from e

    async def put_file(
        self,
        session_id: Optional[str],
        path: str,
        content: bytes,
        user_id: Optional[str] = None,
    ) -> None:
        """Write bytes into this conversation's sidecar workspace."""
        body = {
            "session_id": session_id,
            "user_id": user_id,
            "path": path,
            "content_b64": base64.b64encode(content).decode("ascii"),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{self._base_url}/put_file", json=body)
                resp.raise_for_status()
        except httpx.ConnectError as e:
            raise SandboxConnectError(_connect_error_message()) from e
        except httpx.HTTPStatusError as e:
            text = e.response.text if e.response is not None else str(e)
            raise SandboxError(f"put_file {path} 失败: {text}") from e

    async def get_file(
        self,
        session_id: Optional[str],
        path: str,
        user_id: Optional[str] = None,
    ) -> bytes:
        """Read bytes from this conversation's sidecar workspace."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._base_url}/get_file",
                    json={"session_id": session_id, "user_id": user_id, "path": path},
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.ConnectError as e:
            raise SandboxConnectError(_connect_error_message()) from e
        except httpx.HTTPStatusError as e:
            text = e.response.text if e.response is not None else str(e)
            raise SandboxError(f"get_file {path} 失败: {text}") from e
        try:
            return base64.b64decode(payload.get("content_b64", ""))
        except Exception as e:
            raise SandboxError(f"get_file {path} 返回的 base64 无法解码") from e

    async def get_file_to_path(
        self,
        session_id: Optional[str],
        path: str,
        destination: Path,
        *,
        max_bytes: int,
        user_id: Optional[str] = None,
    ) -> int:
        """Stream a sandbox file from the sidecar into a local path."""
        timeout = httpx.Timeout(
            float(settings.sandbox.max_timeout),
            connect=10.0,
            pool=10.0,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/get_file_raw",
                    json={"session_id": session_id, "user_id": user_id, "path": path},
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        if resp.status_code == 413:
                            match = re.search(r"(\d+)\s*>\s*(\d+)", body)
                            actual = int(match.group(1)) if match else max_bytes + 1
                            server_max = int(match.group(2)) if match else max_bytes
                            raise SandboxFileTooLargeError(
                                actual_size=actual,
                                max_size=min(max_bytes, server_max),
                            )
                        raise SandboxError(f"get_file {path} 失败: HTTP {resp.status_code}: {body}")

                    size_header = resp.headers.get("x-artifact-size") or resp.headers.get(
                        "content-length"
                    )
                    if size_header:
                        try:
                            known_size = int(size_header)
                        except ValueError:
                            known_size = 0
                        if known_size > max_bytes:
                            raise SandboxFileTooLargeError(
                                actual_size=known_size, max_size=max_bytes
                            )
                    return await stream_to_file(
                        resp.aiter_bytes(chunk_size=1024 * 1024),
                        destination,
                        max_bytes=max_bytes,
                    )
        except SandboxError:
            raise
        except httpx.ConnectError as exc:
            raise SandboxConnectError(_connect_error_message()) from exc
        except httpx.TimeoutException as exc:
            raise SandboxTimeoutError(f"get_file {path} 流式读取超时") from exc
        except httpx.HTTPError as exc:
            raise SandboxError(f"get_file {path} 流式读取失败: {exc}") from exc

    async def close_session(self, session_id: Optional[str]) -> None:
        """Delete one conversation workspace without affecting other sessions."""
        if not session_id:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._base_url}/sessions/close",
                    json={"session_id": session_id},
                )
                resp.raise_for_status()
        except Exception as exc:  # protocol requires lifecycle cleanup to never raise
            logger.warning("[script_runner] close_session failed sid=%s: %s", session_id, exc)

    async def touch_session(self, session_id: str) -> bool:
        """Touch an existing conversation workspace."""
        if not session_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._base_url}/sessions/touch",
                    json={"session_id": session_id},
                )
                resp.raise_for_status()
                return bool(resp.json().get("touched"))
        except Exception:
            return False

    async def current_sandbox_id(self, session_id: Optional[str]) -> Optional[str]:
        """Return the stable logical identity of one session workspace."""
        return f"script_runner:{session_id}" if session_id else None

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    # ── Read-only admin interface ─────────────────────────────────────────────────────
    # The sidecar is one container with session-scoped workspaces. It does not expose
    # administrative enumeration, so the security UI still shows one sidecar card.

    def admin_capabilities(self) -> SandboxAdminCapabilities:
        return SandboxAdminCapabilities(provider=self.name)

    async def admin_list_sandboxes(self, include_server: bool = False) -> list[SandboxInfo]:
        raise SandboxAdminNotSupported("script_runner 未开放会话工作区枚举")

    async def admin_get_sandbox(self, sandbox_id: str) -> Optional[SandboxInfo]:
        raise SandboxAdminNotSupported("script_runner 不支持实例详情")

    def admin_pool_stats(self) -> dict:
        raise SandboxAdminNotSupported("script_runner 无连接池")


_SKILL_VIEW_SYNCED: dict[str, float] = {}
_SKILL_VIEW_TTL_S = 60.0


def _refresh_skill_view(user_id: Optional[str]) -> None:
    """Keep this user's skill view (mounted into the sidecar) in step with the shared skills.

    Only shared skills need the refresh — a private skill is materialized straight
    into the user's own dir and is visible at once — so a TTL is enough, and it
    keeps this off the hot path of every bash call. Costs one listdir; failure is
    never worth failing an execution over.
    """
    uid = (user_id or "").strip()
    if not uid:
        return
    now = time.monotonic()
    if now - _SKILL_VIEW_SYNCED.get(uid, 0.0) < _SKILL_VIEW_TTL_S:
        return
    _SKILL_VIEW_SYNCED[uid] = now
    try:
        from core.agent_skills.config import sync_user_skill_view

        sync_user_skill_view(uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[script_runner] 技能视图刷新失败 user=%s: %s", uid, exc)


def _payload_to_result(payload: dict[str, Any]) -> ExecuteResult:
    raw_files = payload.get("files") or []
    files = [
        SandboxFile(
            name=f.get("name", ""),
            size=int(f.get("size") if f.get("size") is not None else 0),
            content_b64=f.get("content_b64", ""),
            mime_type=f.get("mime_type", "application/octet-stream"),
        )
        for f in raw_files
    ]
    ec = payload.get("exit_code")
    elapsed = payload.get("execution_time_ms")
    return ExecuteResult(
        stdout=payload.get("stdout") or "",
        stderr=payload.get("stderr") or "",
        exit_code=int(ec) if ec is not None else -1,
        execution_time_ms=int(elapsed) if elapsed is not None else 0,
        files=files,
    )


def result_to_dict(result: ExecuteResult) -> dict[str, Any]:
    """Serialize an ExecuteResult into a dict equivalent to the old sidecar HTTP response,
    for callers that need to pass through or JSON-encode the result.
    """
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "execution_time_ms": result.execution_time_ms,
        "files": [asdict(f) for f in result.files],
    }
