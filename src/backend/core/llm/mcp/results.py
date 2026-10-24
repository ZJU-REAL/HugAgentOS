"""Lossless MCP result metadata with explicit model-facing content conversion."""
import json
import jsonschema
from agentscope.message import TextBlock, DataBlock, Base64Source
from agentscope.tool import ToolChunk
from agentscope.tool._response import ToolResultState
from mcp import MCPError, types
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT


def content_blocks(contents):
    blocks = []
    for item in contents:
        if isinstance(item, types.TextContent):
            blocks.append(TextBlock(text=item.text))
        elif isinstance(item, (types.ImageContent, types.AudioContent)):
            blocks.append(DataBlock(source=Base64Source(media_type=item.mime_type, data=item.data)))
        else:
            # Resource URIs are references, never automatically fetched as network URLs.
            # Preserve links, embedded resources and their provenance for the model.
            blocks.append(TextBlock(text=item.model_dump_json(by_alias=True, exclude_none=True)))
    return blocks


def _carried_by_text(structured, blocks):
    """Whether a text block already holds the structured value (SDKs wrap non-object returns as {"result": ...})."""
    for block in blocks:
        if not isinstance(block, TextBlock):
            continue
        if structured == {"result": block.text}:
            return True
        try:
            value = json.loads(block.text)
        except ValueError:
            continue
        if value == structured or structured == {"result": value}:
            return True
    return False


def envelope(result):
    """The result's protocol fields without its payload.

    ``content`` becomes the model-facing blocks and ``structuredContent`` is
    carried by them too (see ``tool_result``); a second copy here would only
    double what every result costs to keep, stream and persist.
    """
    return result.model_dump(mode="json", by_alias=True, exclude_none=True,
                             exclude={"content", "structured_content", "contents", "messages"})


def tool_result(result):
    content = content_blocks(result.content)
    metadata = {"mcp": envelope(result)}
    # The model needs the structured value once; skip it only when text already carries it.
    if result.structured_content is not None and not _carried_by_text(result.structured_content, content):
        content.append(TextBlock(text=json.dumps(result.structured_content, ensure_ascii=False,
                                                 separators=(",", ":"))))
    return ToolChunk(content=content, metadata=metadata,
                     state=ToolResultState.ERROR if result.is_error else ToolResultState.RUNNING)


def error_result(text, **metadata):
    return ToolChunk(content=[TextBlock(text=text)], state=ToolResultState.ERROR,
                     metadata={"origin": "mcp", **metadata})


def is_timeout(exc):
    return isinstance(exc, TimeoutError) or (isinstance(exc, MCPError) and exc.code == REQUEST_TIMEOUT)


def poisons_session(exc):
    """A request-level failure leaves the session usable; a lost peer, a changed
    schema or a cancellation does not."""
    if isinstance(exc, MCPError):
        return exc.code == CONNECTION_CLOSED
    return not isinstance(exc, (TimeoutError, jsonschema.ValidationError))
