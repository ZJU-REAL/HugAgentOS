"""HTTP client for managed processes and session-scoped file access."""

from __future__ import annotations

from .process_completion import CompletionMixin

import asyncio
import base64
import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from core.config.settings import settings

from ._common import stream_to_file
from .errors import SandboxConnectError, SandboxError, SandboxFileTooLargeError, SandboxTimeoutError
from .protocol import ProcessRequest, SandboxAdminCapabilities, SandboxAdminNotSupported, SandboxInfo, StagedFile, StageFile
from .runner_auth import auth_headers

logger = logging.getLogger(__name__)


def _connect_error_message() -> str:
    """按部署形态给出准确的连接失败提示。

    本机（DEPLOY_PROFILE=local）下 runner 是子进程而非容器——提示"检查容器"
    纯属误导；看门狗会自动重拉，提示等待/重启客户端即可。
    """
    if settings.deploy.is_local:
        return (
            f"本机代码执行服务不可达（{settings.sandbox.runner_url}），已尝试自动恢复；"
            "稍候重试，若持续失败请重启客户端"
        )
    return "无法连接脚本执行服务 (hugagent-script-runner)，请检查容器是否运行"


class ScriptRunnerProvider(CompletionMixin):
    name = "script_runner"
    # The sidecar runs commands as ordinary host processes, so the OS sandbox is
    # the only isolation boundary there is here.
    runs_on_host = True

    def __init__(self) -> None:
        # Access settings.sandbox.runner_url on each call so test monkeypatching stays effective
        pass

    @property
    def _base_url(self) -> str:
        return settings.sandbox.runner_url


    async def _execution_body(self, req: ProcessRequest) -> dict:
        capability_view_key = None
        from core.capabilities.paths import capabilities_enabled

        if capabilities_enabled() and req.capability_run_id:
            from core.capabilities import runtime
            from core.capabilities.errors import IntegrityFailed

            pinned_view = await asyncio.to_thread(
                runtime.view_for_execution,
                req.capability_run_id,
                str(req.user_id or ""),
                scope_id=req.capability_scope,
            )
            if pinned_view is None:
                raise IntegrityFailed("prepared run is missing")
            capability_view_key = pinned_view.parent.name
        else:
            _refresh_skill_view(req.user_id)
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
            "capability_view_key": capability_view_key,
            # Pass-through: the sidecar applies the prefix and env at spawn time
            # and never decides confinement for itself.
            "sandbox_launch": req.sandbox_launch.to_json() if req.sandbox_launch else None,
        }

        return body

    async def start_process(self, req: ProcessRequest, yield_time_ms: int = 60000) -> dict:
        body = await self._execution_body(req)
        body["capability_run_id"] = req.capability_run_id if body["capability_view_key"] else None
        body["capability_scope"] = req.capability_scope
        body["yield_time_ms"] = max(250, min(yield_time_ms, 60000))
        return await self._process_request("/processes/start", body, body["yield_time_ms"])

    async def write_stdin(
        self, session_id: str, *, sandbox_session_id: str, user_id: Optional[str] = None,
        chars: str = "", yield_time_ms: int = 60000,
    ) -> dict:
        return await self._process_request("/processes/write", {
            "session_id": session_id, "sandbox_session_id": sandbox_session_id,
            "user_id": user_id, "chars": chars,
            "yield_time_ms": max(0, min(yield_time_ms, 300000)),
        }, yield_time_ms)

    async def _process_request(self, route: str, body: dict, wait_ms: int) -> dict:
        # Never retry a start/write: a lost HTTP response is not evidence that
        # the command or the input was not already applied.
        try:
            async with httpx.AsyncClient(
                timeout=max(0, min(wait_ms, 300000)) / 1000 + 30,
                headers=auth_headers(),
            ) as client:
                response = await client.post(self._base_url + route, json=body)
                response.raise_for_status()
                payload = response.json()
                capability = payload.pop("_capability", None)
                if capability and capability.get("run_id"):
                    from core.capabilities import runtime
                    from core.capabilities.errors import IntegrityFailed
                    try:
                        prepared = await asyncio.to_thread(
                            runtime.get, capability["run_id"], scope_id=capability.get("scope", ""),
                        )
                        if prepared is None:
                            raise IntegrityFailed("prepared run is missing")
                        await asyncio.to_thread(
                            runtime.validate, prepared, user_id=capability.get("user_id") or "",
                        )
                    except Exception:
                        if payload.get("session_id"):
                            # Validation failure must not leave modified capability
                            # code running. This cleanup request is never retried.
                            await client.post(self._base_url + "/processes/write", json={
                                "session_id": payload["session_id"],
                                "sandbox_session_id": body.get("sandbox_session_id") or body.get("session_id"),
                                "user_id": capability.get("user_id"),
                                "chars": "\u0003", "yield_time_ms": 0,
                            })
                        raise
                return payload
        except httpx.HTTPStatusError as exc:
            raise SandboxError(f"Process service rejected request: {exc.response.text}") from exc
        except httpx.ConnectError as exc:
            raise SandboxConnectError(_connect_error_message()) from exc
        except httpx.HTTPError as exc:
            raise SandboxError("Process service connection lost; execution state is uncertain. Do not automatically rerun the command.") from exc

    async def stage_files(self, user_id: str, files: list[StageFile]) -> list[StagedFile]:
        body = {
            "user_id": user_id,
            "files": [{"name": f.name, "content_b64": f.content_b64} for f in files],
        }
        try:
            async with httpx.AsyncClient(timeout=30, headers=auth_headers()) as client:
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
            async with httpx.AsyncClient(timeout=30, headers=auth_headers()) as client:
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
            async with httpx.AsyncClient(timeout=30, headers=auth_headers()) as client:
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
            float(settings.sandbox.file_transfer_timeout_s),
            connect=10.0,
            pool=10.0,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=auth_headers()) as client:
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
            async with httpx.AsyncClient(timeout=30, headers=auth_headers()) as client:
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
            async with httpx.AsyncClient(timeout=10, headers=auth_headers()) as client:
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
            async with httpx.AsyncClient(timeout=3, headers=auth_headers()) as client:
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


_SKILL_VIEW_SYNCED: dict[str, tuple[float, int]] = {}
_SKILL_VIEW_TTL_S = 60.0


def _refresh_skill_view(user_id: Optional[str]) -> None:
    """Keep this user's skill view (mounted into the sidecar) in step with the shared skills.

    Only shared skills need the refresh — a private skill is materialized straight
    into the user's own dir and is visible at once — so a TTL is enough, and it
    keeps this off the hot path of every bash call. On a desktop with a capability
    store the view generation changes whenever the store does, and that forces
    an immediate refresh regardless of the TTL. Failure is never worth failing an
    execution over.
    """
    from core.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        from core.agent_skills.publication import prepare_skill_view

        prepare_skill_view(user_id)
        return
    uid = (user_id or "").strip()
    if not uid:
        return
    from core.capabilities.skills import view_generation

    now = time.monotonic()
    gen = view_generation()
    last_ts, last_gen = _SKILL_VIEW_SYNCED.get(uid, (0.0, -1))
    if gen == last_gen and now - last_ts < _SKILL_VIEW_TTL_S:
        return
    _SKILL_VIEW_SYNCED[uid] = (now, gen)
    try:
        from core.agent_skills.config import sync_user_skill_view

        sync_user_skill_view(uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[script_runner] 技能视图刷新失败 user=%s: %s", uid, exc)
