"""Register the shared manager operations inside their owning MCP plugins."""
import asyncio
from typing import Literal
from pydantic import BaseModel, ConfigDict
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from core.services.management_contract import MANAGERS, tools
from core.services.management_errors import management_error


class ArtifactSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["artifact"]
    artifact_id: str


def register(mcp, manager, user_from_context):
    kind = MANAGERS[manager]

    async def call(action, ctx, **arguments):
        from core.services import cloud_management as service
        try:
            return await asyncio.to_thread(getattr(service, action), user_from_context(ctx), kind, **arguments)
        except Exception as exc:
            return {"ok": False, "error": management_error(exc)}

    async def install(source: ArtifactSource, ctx: Context | None = None):
        return await call("install", ctx, source=source.model_dump())

    async def list_installed(ctx: Context | None = None):
        return await call("list_installed", ctx)

    async def get(install_id: str, ctx: Context | None = None):
        return await call("get", ctx, install_id=install_id)

    async def update(install_id: str, source: ArtifactSource, expected_revision: str, ctx: Context | None = None):
        return await call("update", ctx, install_id=install_id, source=source.model_dump(), expected_revision=expected_revision)

    async def uninstall(install_id: str, expected_revision: str, ctx: Context | None = None):
        return await call("uninstall", ctx, install_id=install_id, expected_revision=expected_revision)

    functions = [install, list_installed, get, update, uninstall]
    for spec, fn in zip(tools(manager, "cloud"), functions):
        mcp.add_tool(fn, name=spec["name"], description=spec["description"],
                     annotations=ToolAnnotations(**spec["annotations"]), meta=spec["_meta"])
