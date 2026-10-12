"""Recheck project authorization immediately before tool I/O."""

import asyncio

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolChunk
from core.llm.project_environment import validate_project_environment
from fastapi import HTTPException


class ProjectEnvironmentMiddleware(MiddlewareBase):
    def __init__(self, request):
        self.request = request

    async def on_acting(self, agent, input_kwargs, next_handler):
        try:
            await asyncio.to_thread(validate_project_environment, self.request)
        except HTTPException as error:
            yield ToolChunk(
                content=[TextBlock(type="text", text=str(error.detail))],
                state=ToolResultState.DENIED,
                metadata={"project_environment_denied": True},
            )
            return
        async for item in next_handler(**input_kwargs):
            yield item
