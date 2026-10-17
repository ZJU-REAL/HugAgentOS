"""Responses for Personal Agent."""

import json

from agentscope.message import TextBlock

from agentscope.tool._response import ToolChunk as ToolResponse

def response(value):
    return ToolResponse(content=[TextBlock(type="text", text=json.dumps(value, ensure_ascii=False, default=str))])
