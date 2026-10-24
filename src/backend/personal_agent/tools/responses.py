"""Responses for Personal Agent."""

import json

from agentscope.message import TextBlock

from agentscope.tool._response import ToolChunk

def response(value):
    return ToolChunk(content=[TextBlock(type="text", text=json.dumps(value, ensure_ascii=False, default=str))])
