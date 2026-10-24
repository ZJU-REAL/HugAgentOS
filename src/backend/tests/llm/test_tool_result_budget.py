"""A tool result spends the context budget on what the model receives, and only that.

From 2026-10-22 every MCP result also carried its raw ``CallToolResult`` in
``metadata["mcp"]`` — a second and third copy of the payload. The assembler
counted that metadata against the result's 20k allowance but could only shorten
``output``, so any result over ~33k characters was excluded outright: the
request kept the tool call and lost its output, and the model, seeing a call
that returned nothing, stopped after one character or repeated itself until
cancelled.
"""

from __future__ import annotations

import asyncio
import json

from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from mcp import types

from core.llm.context.adapter import AgentScopeContextAdapter
from core.llm.context.content import estimate_context_tokens
from core.llm.context.ir import ContextAssembler
from core.llm.context.items import ContextItem
from core.llm.mcp.results import tool_result
from core.models.responses import ResponsesReplayFormatter

# Shape of a real web_fetch result: a JSON envelope around an escaped JSON page.
_RECORD = {"vnum": "283001", "vName": "Abu", "country": "Japan", "latitude": 34.5, "longitude": 131.6}
_PAGE = json.dumps([_RECORD] * 1200, separators=(",", ":"))


def _web_fetch_text(chars: int) -> str:
    return json.dumps({"result": _PAGE[:chars], "returned_chars": chars}, indent=2)


def _mcp_metadata(text: str) -> dict:
    """What results.py stored before the fix: the payload twice more."""
    return {"mcp": {"content": [{"type": "text", "text": text}],
                    "structuredContent": json.loads(text), "isError": False}}


def _messages(text: str, metadata: dict) -> list[Msg]:
    return [
        Msg(name="user", role="user", content=[TextBlock(type="text", text="拉取火山列表")]),
        Msg(name="agent", role="assistant", content=[
            ToolCallBlock(type="tool_call", id="call_1", name="web_fetch", input="{}"),
            ToolResultBlock(type="tool_result", id="call_1", name="web_fetch",
                            output=[TextBlock(type="text", text=text)], state="success",
                            metadata=metadata),
        ]),
    ]


def _assemble(messages: list[Msg]):
    adapter = AgentScopeContextAdapter()
    assembly = ContextAssembler(total_budget=500_000).assemble(adapter.items_from_messages(messages))
    entry = next(e for e in assembly.manifest["included"] + assembly.manifest["excluded"]
                 if e["kind"] == "tool_result")
    request = asyncio.run(ResponsesReplayFormatter().format(adapter.messages_from_items(assembly.included)))
    outputs = [item["output"] for item in request if item.get("type") == "function_call_output"]
    return entry, outputs


def test_metadata_never_spends_the_model_budget():
    text = _web_fetch_text(20_000)
    block = {"type": "tool_result", "id": "call_1", "name": "web_fetch", "state": "success",
             "output": [{"type": "text", "text": text}]}
    assert estimate_context_tokens({**block, **_mcp_metadata(text)}) == estimate_context_tokens(block)


def test_large_mcp_result_is_truncated_and_reaches_the_model():
    # 80k characters with the raw-envelope metadata was excluded (90k "tokens", final 0).
    text = _web_fetch_text(80_000)
    entry, outputs = _assemble(_messages(text, _mcp_metadata(text)))

    assert entry["action"] == "truncated"
    assert entry["final_tokens"] <= 20_000
    assert len(outputs) == 1 and outputs[0].startswith('{\n  "result": "[{\\"vnum\\"')


def test_mid_size_result_is_kept_whole_regardless_of_metadata():
    # 30k characters fits the allowance; metadata used to cut it to a third.
    text = _web_fetch_text(30_000)
    entry, outputs = _assemble(_messages(text, _mcp_metadata(text)))

    assert entry["action"] == "included"
    assert outputs == [text]


def test_unfittable_tool_result_becomes_a_placeholder_not_a_missing_output():
    call = ContextItem.create(
        item_id="call", kind="tool_call", origin="agent:tool_call", trust="assistant",
        visibility="model", priority=700, token_budget=0, truncation_policy="never",
        content={"type": "tool_call", "id": "t1", "name": "web_fetch", "input": "{}"},
        cache_class="dynamic", created_seq=2, render_role="assistant", pair_id="t1",
        message_group="message:2", unit_id="step:2",
    )
    result = ContextItem.create(
        item_id="result", kind="tool_result", origin="tool:result", trust="tool",
        visibility="model", priority=700, token_budget=50, truncation_policy="drop",
        content={"type": "tool_result", "id": "t1", "name": "web_fetch", "state": "success",
                 "output": "x" * 4_000},
        cache_class="dynamic", created_seq=3, render_role="assistant", pair_id="t1",
        message_group="message:2", unit_id="step:2",
    )
    assembly = ContextAssembler(total_budget=10_000).assemble([call, result])

    kept = {item.item_id: item for item in assembly.included}
    assert set(kept) == {"call", "result"}
    assert "tool output pruned" in str(kept["result"].content["output"])
    entry = next(e for e in assembly.manifest["included"] if e["item_id"] == "result")
    assert (entry["action"], entry["reason"]) == ("pruned", "item_budget")


def test_mcp_metadata_keeps_the_envelope_without_the_payload():
    text = _web_fetch_text(5_000)
    raw = types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=json.loads(text),
        isError=False,
    )
    chunk = tool_result(raw)

    assert [block.text for block in chunk.content] == [text]
    assert chunk.metadata["mcp"]["isError"] is False
    assert "content" not in chunk.metadata["mcp"]
    assert "structuredContent" not in chunk.metadata["mcp"]
