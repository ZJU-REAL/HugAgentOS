"""Authorized cloud gateway invocation, separate from manifest discovery."""
from core.harness.registry import execution_identity as bridge_identity
import asyncio
import weakref
from contextlib import asynccontextmanager
from typing import Any, Dict
import httpx
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

_shared_clients = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _http(transport):
    """Reuse one pooled client per event loop; an injected transport gets its own client."""
    if transport is not None:
        async with httpx.AsyncClient(transport=transport) as client:
            yield client
        return
    loop = asyncio.get_running_loop()
    client = _shared_clients.get(loop)
    if client is None or client.is_closed:
        client = _shared_clients[loop] = httpx.AsyncClient()
    yield client


class GatewayMCPTool(ToolBase):
    """MCP-shaped tool built from a cloud-owned runtime manifest snapshot."""

    is_mcp = True
    is_state_injected = False
    is_external_tool = False
    is_concurrency_safe = False

    def __init__(
        self,
        *,
        mcp_name: str,
        tool: dict,
        invoke_url: str,
        schema_hash: str,
        headers: Dict[str, str],
        timeout: float,
        transport: Any = None,
        source_plugin: str = "",
    ) -> None:
        super().__init__()
        self._source_plugin = source_plugin
        self.mcp_name = mcp_name
        self.name = tool["name"]
        self.description = tool.get("description") or ""
        schema = dict(tool.get("inputSchema") or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("required", [])
        self.input_schema = schema
        self.is_read_only = bool(
            tool.get("annotations", {}).get("readOnlyHint")
        )
        self._invoke_url = invoke_url
        self._schema_hash = schema_hash
        self._headers = dict(headers)
        self._timeout = max(1.0, float(timeout or 120.0))
        self._transport = transport

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        if self.is_read_only:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="This is a read-only MCP tool. Allowing execution.",
            )
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message="MCP tools must be explicitly allowed by the user.",
        )

    def _unknown_outcome(self) -> ToolChunk:
        from agentscope.message import TextBlock, ToolResultState

        return ToolChunk(
            content=[TextBlock(text="云端写操作的结果未知，已停止任务。请先核对云端结果，避免重复执行。")],
            state=ToolResultState.ERROR,
            metadata={"origin": "cloud", "mcp_server_id": self.mcp_name,
                      "gateway_outcome_unknown": True},
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        from extensions.mcp_servers.registry import permission_specs
        from core.sandbox.permissions.dispatch_models import CURRENT_PERMISSION_TICKET, PermissionEnforcementError, _json_hash
        spec = permission_specs(self.mcp_name).get(self.name)
        if spec is not None:
            ticket = CURRENT_PERMISSION_TICKET.get()
            if (ticket is None or not ticket.active or ticket.tool_name != self.name
                    or ticket.spec_key != spec.key or ticket.args_hash != _json_hash(kwargs)):
                raise PermissionEnforcementError("云端空间工具缺少匹配的预执行授权票据")
        headers = dict(self._headers)
        from core.harness.registry.execution_identity import context_headers
        headers.update(context_headers())
        from core.harness.registry.run_capabilities import has_run_capabilities
        captured = None
        if has_run_capabilities() and "/api/v1/desktop/capability/gateway/" in self._invoke_url:
            captured = {"cloud_base": self._invoke_url.split("/api/v1/desktop/capability/gateway/", 1)[0],
                        "token": str(headers.get("Authorization") or "").removeprefix("Bearer ")}
            bridge_identity.require_current_account(captured)
            headers.update(bridge_identity.cloud_headers(bridge_identity.get_state()))
        local_result = None
        if captured is not None:
            from core.harness.registry.tool_operations import local_tool_result
            async def authorize_local():
                async with _http(self._transport) as client:
                    reply = await client.post(
                        self._invoke_url.rsplit("/", 1)[0] + "/authorize",
                        headers=headers, timeout=15.0,
                        json={"tool_name": self.name, "schema_hash": self._schema_hash,
                              "operation_id": kwargs.get("tool_effect_id") or "authorization"},
                    )
                bridge_identity.require_current_account(captured)
                if reply.status_code in (401, 403):
                    with bridge_identity.account_scope(captured):
                        bridge_identity.clear_state()
                if not reply.is_success:
                    raise ValueError("本机任务工具授权已变更或不可用，请同步云端能力后重试")
            try:
                local_result = await local_tool_result(
                    self._source_plugin, self.name, kwargs, headers, authorize=authorize_local,
                )
            except ValueError as exc:
                from agentscope.message import TextBlock, ToolResultState
                return ToolChunk(content=[TextBlock(text=str(exc))], state=ToolResultState.ERROR)
            bridge_identity.require_current_account(captured)
            if local_result is not None and not local_result.metadata.get("merge_cloud"):
                return local_result
        headers["accept-encoding"] = "identity"
        timeout = httpx.Timeout(
            connect=min(10.0, self._timeout),
            read=self._timeout,
            write=min(60.0, self._timeout),
            pool=min(10.0, self._timeout),
        )
        channel = None
        if captured is not None:
            from core.harness.registry.transfer_channels import UPLOAD_OPTIONS_HEADER, UPLOAD_SCHEMA_HEADER, upload_channel

            channel = upload_channel(self._source_plugin, self.name)
        if channel is not None:
            body, options = await channel.package(kwargs, headers)
            bridge_identity.require_current_account(captured)
        from core.harness.registry.tool_operations import receipt_operations
        from core.harness.tool_effect_context import CURRENT_TOOL_EFFECT
        effect = CURRENT_TOOL_EFFECT.get()
        operations = receipt_operations(self.name)
        if operations is not None and effect is not None:
            await asyncio.to_thread(operations.remember, effect, self.mcp_name,
                                    self._invoke_url, self._schema_hash)
        try:
            async with _http(self._transport) as client:
                if channel is not None:
                    headers.update(
                        {
                            "content-type": channel.content_type,
                            UPLOAD_OPTIONS_HEADER: options,
                            UPLOAD_SCHEMA_HEADER: self._schema_hash,
                        }
                    )
                    response = await client.post(
                        f"{self._invoke_url.rsplit('/', 1)[0]}/{channel.endpoint}",
                        headers=headers,
                        content=body,
                        timeout=timeout,
                    )
                else:
                    response = await client.post(
                        self._invoke_url,
                        headers=headers,
                        json={
                            "tool_name": self.name,
                            "arguments": kwargs,
                            "schema_hash": self._schema_hash,
                        },
                        timeout=timeout,
                    )
            if captured is not None:
                bridge_identity.require_current_account(captured)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ValueError("cloud gateway returned no tool result")
            if captured is not None:
                from core.harness.registry.transfer_channels import result_localizer

                localize = result_localizer(self._source_plugin, self.name)
                if localize is not None:
                    with bridge_identity.account_scope(captured):
                        localize(data, captured["cloud_base"], kwargs, headers)
            chunk = ToolChunk.model_validate(data)
            chunk.metadata.setdefault("origin", "cloud")
            chunk.metadata.setdefault("mcp_server_id", self.mcp_name)
            if local_result is not None and local_result.metadata.get("merge_cloud"):
                from agentscope.message import TextBlock
                chunk.content = [TextBlock(text="云端任务："), *chunk.content,
                                 TextBlock(text="本机任务："), *local_result.content]
                chunk.metadata["origin"] = "mixed"
            return chunk
        except httpx.TimeoutException as exc:
            if not self.is_read_only:
                return self._unknown_outcome()
            raise RuntimeError(f"云端工具 {self.name} 调用超时，请稍后重试") from exc
        except httpx.HTTPStatusError as exc:
            if not self.is_read_only and (exc.response.status_code >= 500 or exc.response.status_code == 422):
                return self._unknown_outcome()
            if captured is not None and exc.response.status_code in (401, 403):
                with bridge_identity.account_scope(captured):
                    bridge_identity.clear_state()
            if exc.response.status_code == 409:
                # 云端在此明确告知能力已变——这是「不轮询」下发现云端改动的信号之一，
                # 立刻后台同步一次清单，下一轮对话就是新的。
                if captured is not None:
                    bridge_identity.notify_cloud_changed()
                raise RuntimeError(
                    f"云端工具 {self.name} 已更新，正在同步最新能力，请重试"
                ) from exc
            raise RuntimeError(
                f"云端工具 {self.name} 暂时不可用（HTTP {exc.response.status_code}）"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            if not self.is_read_only:
                return self._unknown_outcome()
            raise RuntimeError(f"云端工具 {self.name} 返回异常，请稍后重试") from exc
