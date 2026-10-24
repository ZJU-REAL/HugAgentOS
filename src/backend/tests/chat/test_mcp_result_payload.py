"""MCP tool cards get the tool's own value, not the CallToolResult envelope.

From 2026-10-22 the stream pushed ``metadata["mcp"]`` (the whole CallToolResult)
as the card result, so renderers keyed on the tool's fields found nothing (the
search card showed "暂无搜索结果"). Cards render the model-facing text again; the
structured value only stands in when that text is not JSON.
"""

import asyncio
import json
from typing import Any, Dict

from mcp.server.mcpserver import MCPServer

from core.chat.tool_payloads import _build_mcp_result_payload, _sdk_wrapped_output

DATASETS = {"public_datasets": [], "private_datasets": [], "total": 0}
SEARCH = {"result": {"status": "complete", "results": [{"title": "Doc", "url": "https://example.org"}]}}


def _schemas() -> dict:
    server = MCPServer("schemas")

    @server.tool()
    def typing_dict() -> Dict[str, Any]:
        return {}

    @server.tool()
    def builtin_dict() -> dict[str, Any]:
        return {}

    @server.tool()
    def text() -> str:
        return ""

    return {tool.name: tool.output_schema for tool in asyncio.run(server.list_tools())}


def _envelope(structured) -> dict:
    return {"content": [], "isError": False, "structuredContent": structured}


def test_sdk_wrap_detection_matches_the_installed_sdk():
    schemas = _schemas()
    assert _sdk_wrapped_output(schemas["typing_dict"]) is True
    assert _sdk_wrapped_output(schemas["text"]) is True
    assert _sdk_wrapped_output(schemas["builtin_dict"]) is False
    assert _sdk_wrapped_output({"x-fastmcp-wrap-result": True}) is True
    assert _sdk_wrapped_output(None) is False


def test_complete_text_keeps_the_shape_and_citation_anchors():
    annotated = {"result": {"results": [{"title": "Doc", "cite_id": "e1"}]}}
    text = json.dumps(annotated)
    assert _build_mcp_result_payload(_envelope(SEARCH), None, text, annotated) == annotated
    # A typing.Dict tool: structuredContent is wrapped, the text is the tool's own value.
    wrapped_schema = _schemas()["typing_dict"]
    assert (
        _build_mcp_result_payload(_envelope({"result": DATASETS}), wrapped_schema, json.dumps(DATASETS), DATASETS)
        == DATASETS
    )


def test_truncated_text_falls_back_to_the_unwrapped_structured_value():
    schemas = _schemas()
    clipped = '{"public_datasets": [〔截断'
    legacy = {"result": clipped}
    assert _build_mcp_result_payload(_envelope({"result": DATASETS}), schemas["typing_dict"], clipped, legacy) == DATASETS
    assert _build_mcp_result_payload(_envelope(SEARCH), schemas["builtin_dict"], clipped, legacy) == SEARCH
    # A wrapped string keeps the {"result": ...} shape the text path produces.
    assert _build_mcp_result_payload(_envelope({"result": "full"}), schemas["text"], "fu", {"result": "fu"}) == {
        "result": "full"
    }


def test_tools_without_structured_content_keep_the_parsed_text():
    envelope = {"content": [{"type": "text", "text": "plain"}], "isError": False}
    assert _build_mcp_result_payload(envelope, None, "plain", {"result": "plain"}) == {"result": "plain"}
