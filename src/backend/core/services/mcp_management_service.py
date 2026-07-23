"""Shared MCP mutation helpers used by admin and self-service routes."""

from __future__ import annotations

import asyncio
import logging

from core.db.models import AdminMcpServer
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
_PROBE_TIMEOUT_S = 10.0


def refresh_mcp_caches() -> None:
    """Invalidate MCP, catalog, capability, and prompt caches after a mutation."""
    invalidators = []
    try:
        from core.services.mcp_service import McpServerConfigService

        invalidators.append(McpServerConfigService.get_instance().invalidate_cache)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unable to load the MCP cache invalidator: %s", exc)

    for module_name, function_name in (
        ("core.config.catalog_loader", "invalidate_catalog_cache"),
        ("core.config.catalog_runtime", "invalidate_runtime_catalog_cache"),
        ("core.config.catalog_resolver", "invalidate_capability_cache"),
        ("prompts.prompt_runtime", "invalidate_prompt_cache"),
    ):
        try:
            module = __import__(module_name, fromlist=[function_name])
            invalidators.append(getattr(module, function_name))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Unable to load cache invalidator %s.%s: %s", module_name, function_name, exc
            )

    for invalidate in invalidators:
        try:
            invalidate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP cache invalidation failed: %s", exc)


async def probe_mcp_connectivity(
    row: AdminMcpServer,
    db: Session | None = None,
) -> tuple[bool, str]:
    """Return whether an MCP server can connect and enumerate its tools."""
    from core.services.mcp_service import McpServerConfigService

    cfg = McpServerConfigService.get_instance()._row_to_config(row)

    async def _do_probe() -> None:
        from core.llm.mcp_pool import make_client

        if row.transport not in ("stdio", "streamable_http", "sse"):
            raise RuntimeError(f"Unknown transport: {row.transport}")
        client = make_client(row.server_id, cfg)
        await client.connect()
        try:
            discovered = await client.list_tools()
            tools_meta = []
            for tool in discovered or []:
                input_schema = (
                    getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
                )
                if hasattr(input_schema, "model_dump"):
                    input_schema = input_schema.model_dump(mode="json")
                tools_meta.append(
                    {
                        "name": tool.name,
                        "description": getattr(tool, "description", "") or "",
                        "inputSchema": input_schema,
                    }
                )
            row.tools_json = tools_meta
            if db is not None and tools_meta:
                from core.ontology.build_validator import OntologyBuildValidator

                extra_config = row.extra_config or {}
                ontology_tags = {
                    str(tag).strip()
                    for tag in (extra_config.get("ontology_tags") or [])
                    if str(tag).strip()
                }
                tool_tags = extra_config.get("tool_tags")
                if isinstance(tool_tags, dict):
                    ontology_tags.update(
                        str(tag).strip()
                        for tags in tool_tags.values()
                        if isinstance(tags, list)
                        for tag in tags
                        if str(tag).strip()
                    )
                report = OntologyBuildValidator(db).validate(
                    asset_type="tool",
                    name=row.display_name,
                    description=row.description or "",
                    tool_names=[item["name"] for item in tools_meta],
                    tool_schemas={item["name"]: item["inputSchema"] for item in tools_meta},
                    ontology_tags=sorted(ontology_tags),
                )
                if not report.valid:
                    messages = "; ".join(item.message for item in report.errors)
                    raise RuntimeError(f"Domain Pack 构建校验失败：{messages}")
        finally:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP probe client close failed: %s", exc)

    try:
        await asyncio.wait_for(_do_probe(), timeout=_PROBE_TIMEOUT_S)
        return True, ""
    except asyncio.TimeoutError:
        return False, f"连接超时（> {_PROBE_TIMEOUT_S:.0f}s）"
    except BaseException as exc:
        return False, f"{type(exc).__name__}: {exc}"
