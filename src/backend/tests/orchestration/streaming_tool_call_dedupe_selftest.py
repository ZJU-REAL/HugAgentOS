"""Standalone regression check for streamed tool-call arguments.

The May 2026 downgrade swallowed every partial argument event after an older
adapter repeatedly emitted cumulative tool payloads and created hundreds of
cards.  The current contract is stricter: one start event opens a card, batched
deltas update it by stable ``tool_id``, and one completed call supplies parsed
arguments.

Run from the repository root:
  PYTHONPATH=src/backend python -m tests.orchestration.streaming_tool_call_dedupe_selftest
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any


class ToolCallStartEvent:  # name-dispatched by StreamingAgent._map_event
    def __init__(self, tool_id: str, tool_name: str) -> None:
        self.tool_call_id = tool_id
        self.tool_call_name = tool_name


class ToolCallDeltaEvent:
    def __init__(self, tool_id: str, delta: str) -> None:
        self.tool_call_id = tool_id
        self.delta = delta


class ToolCallEndEvent:
    def __init__(self, tool_id: str) -> None:
        self.tool_call_id = tool_id


async def _collect(events: list[Any]) -> list[tuple[str, Any]]:
    import orchestration.streaming as streaming_mod
    from orchestration.streaming import StreamingAgent

    async def reply_stream(inputs=None):  # noqa: ANN001, ARG001
        for event in events:
            yield event

    state = SimpleNamespace(
        user_id="tester",
        chat_id="dedupe_case",
        apply_request_context=lambda ctx, text: None,
        context=SimpleNamespace(extend=lambda messages: None),
    )
    agent = SimpleNamespace(state=state, model=None, reply_stream=reply_stream)

    # Make the character threshold deterministic for this standalone test; the
    # production time threshold remains covered by the pytest suite.
    streaming_mod._TOOL_CALL_DELTA_FLUSH_CHARS = 256
    streaming_mod._TOOL_CALL_DELTA_FLUSH_INTERVAL_S = 999.0

    output: list[tuple[str, Any]] = []
    streamer = StreamingAgent(agent, mcp_clients=[])
    async for item in streamer.stream(session_messages=[], context={"enable_thinking": True}):
        output.append(item)
    return output


def main() -> int:
    tool_id = "call_repro_001"
    tool_name = "bash"
    full_json = json.dumps({"command": "echo " + ("x" * 4000)})
    upstream = [ToolCallStartEvent(tool_id, tool_name)]
    upstream.extend(ToolCallDeltaEvent(tool_id, char) for char in full_json)
    upstream.append(ToolCallEndEvent(tool_id))

    events = asyncio.run(_collect(upstream))
    starts = [payload for kind, payload in events if kind == "tool_call_start"]
    deltas = [payload for kind, payload in events if kind == "tool_call_delta"]
    calls = [payload for kind, payload in events if kind == "tool_call"]

    errors: list[str] = []
    if starts != [{"name": tool_name, "id": tool_id}]:
        errors.append(f"expected one stable start event, got {starts!r}")
    if "".join(item["delta"] for item in deltas) != full_json:
        errors.append("batched deltas did not reconstruct the original argument JSON")
    max_expected_deltas = (len(full_json) + 255) // 256
    if len(deltas) > max_expected_deltas:
        errors.append(
            f"delta batching regressed: {len(deltas)} events for {len(full_json)} characters"
        )
    if len(calls) != 1 or calls[0].get("args") != json.loads(full_json):
        errors.append(f"expected one completed parsed call, got {calls!r}")

    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1

    print(
        "streaming_tool_call_dedupe_selftest: OK "
        f"(argument_chars={len(full_json)}, delta_events={len(deltas)}, tool_cards=1)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
