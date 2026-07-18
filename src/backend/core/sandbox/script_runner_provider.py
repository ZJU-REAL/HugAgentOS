"""ScriptRunnerProvider — wraps HTTP calls to the existing hugagent-script-runner container.

Behavior:
- 2 ReadTimeout retries
- ``http_timeout = req.timeout + 30`` as the outer HTTP timeout
- HTTPStatusError / ConnectError / TimeoutException classified and mapped to SandboxError subclasses
"""

from __future__ import annotations

import base64
import logging
from dataclasses import asdict
from typing import Any, Optional

import httpx

from core.config.settings import settings

from .errors import SandboxConnectError, SandboxError, SandboxTimeoutError
from .protocol import (
    ExecuteRequest,
    ExecuteResult,
    SandboxAdminCapabilities,
    SandboxAdminNotSupported,
    SandboxFile,
    SandboxInfo,
    StageFile,
    StagedFile,
)

logger = logging.getLogger(__name__)


class ScriptRunnerProvider:
    name = "script_runner"

    def __init__(self) -> None:
        # Access settings.sandbox.runner_url on each call so test monkeypatching stays effective
        pass

    @property
    def _base_url(self) -> str:
        return settings.sandbox.runner_url

    async def execute(self, req: ExecuteRequest) -> ExecuteResult:
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
                        req.script_name, attempt + 1, http_timeout,
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
                    raise SandboxConnectError(
                        "无法连接脚本执行服务 (hugagent-script-runner)，请检查容器是否运行"
                    ) from e

        raise SandboxTimeoutError(
            f"脚本执行读取超时（{http_timeout}秒，已重试）: "
            f"{type(last_exc).__name__ if last_exc else 'ReadTimeout'}"
        )

    async def stage_files(
        self, user_id: str, files: list[StageFile]
    ) -> list[StagedFile]:
        body = {
            "user_id": user_id,
            "files": [{"name": f.name, "content_b64": f.content_b64} for f in files],
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{self._base_url}/stage", json=body)
                resp.raise_for_status()
                staged_raw = resp.json().get("staged", [])
                return [
                    StagedFile(name=item["name"], path=item["path"])
                    for item in staged_raw
                ]
        except httpx.ConnectError as e:
            raise SandboxConnectError("无法连接脚本执行服务") from e
        except httpx.HTTPStatusError as e:
            text = e.response.text if e.response is not None else str(e)
            raise SandboxError(f"暂存文件失败: {text}") from e

    async def put_file(
        self, session_id: Optional[str], path: str, content: bytes,
        user_id: Optional[str] = None,
    ) -> None:
        """Write bytes into the sandbox via the sidecar's /put_file.

        ``session_id`` / ``user_id`` are ignored under the script_runner provider — the
        sidecar's ``/workspace`` is globally shared for the container's lifetime, with no
        notion of a "session"/user binding.
        """
        del session_id, user_id
        body = {
            "path": path,
            "content_b64": base64.b64encode(content).decode("ascii"),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{self._base_url}/put_file", json=body)
                resp.raise_for_status()
        except httpx.ConnectError as e:
            raise SandboxConnectError("无法连接脚本执行服务") from e
        except httpx.HTTPStatusError as e:
            text = e.response.text if e.response is not None else str(e)
            raise SandboxError(f"put_file {path} 失败: {text}") from e

    async def get_file(
        self, session_id: Optional[str], path: str,
        user_id: Optional[str] = None,
    ) -> bytes:
        """Read sandbox file bytes via the sidecar's /get_file. ``session_id`` / ``user_id`` ignored."""
        del session_id, user_id
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._base_url}/get_file", json={"path": path}
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.ConnectError as e:
            raise SandboxConnectError("无法连接脚本执行服务") from e
        except httpx.HTTPStatusError as e:
            text = e.response.text if e.response is not None else str(e)
            raise SandboxError(f"get_file {path} 失败: {text}") from e
        try:
            return base64.b64decode(payload.get("content_b64", ""))
        except Exception as e:
            raise SandboxError(f"get_file {path} 返回的 base64 无法解码") from e

    async def close_session(self, session_id: Optional[str]) -> None:
        """No-op: the sidecar's ``/workspace`` is globally shared; there is no per-session sandbox to destroy."""
        del session_id

    async def current_sandbox_id(self, session_id: Optional[str]) -> Optional[str]:
        """The script_runner sidecar's ``/workspace`` is global to the container
        and persists for the container's lifetime. There is no "per-session
        sandbox" to invalidate, so we return a constant — callers tracking
        sandbox identity will treat the value as stable and skill files
        materialized once never need to be re-pushed.
        """
        del session_id
        return "script_runner"

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    # ── Read-only admin interface ─────────────────────────────────────────────────────
    # The sidecar is a single-container global /workspace with no notion of an "instance" —
    # it only exposes a capability declaration; everything else raises
    # SandboxAdminNotSupported, which the security admin backend downgrades to a single "shared sidecar" card.

    def admin_capabilities(self) -> SandboxAdminCapabilities:
        return SandboxAdminCapabilities(provider=self.name)

    async def admin_list_sandboxes(
        self, include_server: bool = False
    ) -> list[SandboxInfo]:
        raise SandboxAdminNotSupported("script_runner 是共享 sidecar，无可枚举实例")

    async def admin_get_sandbox(self, sandbox_id: str) -> Optional[SandboxInfo]:
        raise SandboxAdminNotSupported("script_runner 不支持实例详情")

    def admin_pool_stats(self) -> dict:
        raise SandboxAdminNotSupported("script_runner 无连接池")


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
