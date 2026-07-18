"""Selftest: StreamingAgent should emit exactly one tool_call per tool_id
even when AgentScope feeds many partial chunks (MiniMax/Qwen stream long
tool_call args as 100s of cumulative chunks).

Repro setup:
  - Feed N-1 msgs with is_last=False, each carrying a ToolUseBlock with the
    same tool_id and progressively-larger input.
  - Then feed 1 msg with is_last=True (the "final" form of the same tool_use).
  - Then feed a system msg with a matching ToolResultBlock, is_last=True.

Expected events:
  - 0 tool_call events while is_last=False
  - Exactly 1 tool_pending event when the partial stream starts
  - Exactly 1 tool_call event when is_last=True arrives (with final args)
  - Exactly 1 tool_result event after the ToolResultBlock msg

Run:
  PYTHONPATH=src/backend python -m tests.streaming_tool_call_dedupe_selftest
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Tuple
from unittest.mock import AsyncMock, MagicMock


def main() -> int:
    try:
        from orchestration.streaming import StreamingAgent
        from agentscope.message import Msg
        from agentscope.message._message_block import ToolUseBlock, ToolResultBlock
    except ModuleNotFoundError as e:
        print(f"streaming_tool_call_dedupe_selftest: SKIP (missing dependency: {e})")
        return 0

    HEREDOC_FULL = "echo " + ("x" * 4000)
    PARTIAL_CHUNKS = 50  # enough to demonstrate the storm; real MiniMax produced 589
    TOOL_ID = "call_repro_001"
    TOOL_NAME = "bash"

    async def _run() -> List[Tuple[str, Any]]:
        # Build a fake agent: minimal surface, just enough for StreamingAgent.
        agent = MagicMock()
        agent._disable_console_output = True
        agent._jx_context = None
        agent._instance_pre_reply_hooks = {}
        agent.memory = MagicMock()
        agent.memory.add = AsyncMock()

        # Real asyncio.Queue so the stream loop polls it the same way as production.
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        agent.msg_queue = queue
        agent.set_msg_queue_enabled = MagicMock()

        async def _push_sequence(_user_msg: Any) -> Any:
            """Mimic AgentScope's _reasoning loop: many partial chunks + a final
            is_last=True chunk, then a tool_result msg."""
            # Partial chunks: build cumulative input growing toward the full
            # heredoc. Same tool_id throughout (matches OpenAI streaming spec).
            for i in range(PARTIAL_CHUNKS):
                progress = int(len(HEREDOC_FULL) * (i + 1) / (PARTIAL_CHUNKS + 1))
                partial_input = {"cmd": HEREDOC_FULL[:progress]}
                msg = Msg(name="agent", role="assistant", content=[
                    ToolUseBlock(type="tool_use", id=TOOL_ID, name=TOOL_NAME, input=partial_input),
                ])
                await queue.put((msg, False, None))  # is_last=False
                await asyncio.sleep(0)  # yield to consumer

            # Final chunk with complete args, is_last=True.
            final_msg = Msg(name="agent", role="assistant", content=[
                ToolUseBlock(type="tool_use", id=TOOL_ID, name=TOOL_NAME, input={"cmd": HEREDOC_FULL}),
            ])
            await queue.put((final_msg, True, None))

            # tool_result msg (system role).
            result_msg = Msg(name="system", role="system", content=[
                ToolResultBlock(type="tool_result", id=TOOL_ID, name=TOOL_NAME, output=[{"text": "ok"}]),
            ])
            await queue.put((result_msg, True, None))

            # Final assistant text msg signalling "done" (no tool_use).
            done_msg = Msg(name="agent", role="assistant", content="done")
            await queue.put((done_msg, True, None))
            return done_msg

        agent.reply = _push_sequence

        streamer = StreamingAgent(agent, mcp_clients=[])
        events: List[Tuple[str, Any]] = []
        async for ev in streamer.stream(
            session_messages=[{"role": "user", "content": "run it"}],
            context={"chat_id": "dedupe_case", "user_id": "tester"},
        ):
            events.append(ev)

        return events

    events = asyncio.run(_run())

    # Filter to only the event types we care about.
    tool_calls = [(k, v) for k, v in events if k == "tool_call"]
    tool_pendings = [(k, v) for k, v in events if k == "tool_pending"]
    tool_results = [(k, v) for k, v in events if k == "tool_result"]

    fail = False

    if len(tool_calls) != 1:
        print(f"FAIL: expected exactly 1 tool_call event, got {len(tool_calls)}")
        for ev in tool_calls[:3]:
            print(f"  {ev!r}")
        fail = True
    else:
        final_args = tool_calls[0][1].get("args", {})
        if final_args.get("cmd") != HEREDOC_FULL:
            print(f"FAIL: tool_call args not the final form (len={len(final_args.get('cmd', ''))} vs {len(HEREDOC_FULL)})")
            fail = True

    # We expect at least 1 tool_pending (during the partial stream).
    if len(tool_pendings) < 1:
        print(f"FAIL: expected >= 1 tool_pending event, got {len(tool_pendings)}")
        fail = True
    elif tool_pendings[0][1].get("reason") != "tool_args_streaming":
        print(f"FAIL: tool_pending reason should be 'tool_args_streaming', got {tool_pendings[0][1]!r}")
        fail = True

    if len(tool_results) != 1:
        print(f"FAIL: expected exactly 1 tool_result event, got {len(tool_results)}")
        fail = True

    if fail:
        print(f"\nAll events ({len(events)} total):")
        for k, v in events:
            preview = repr(v)
            if len(preview) > 120:
                preview = preview[:117] + "..."
            print(f"  {k}: {preview}")
        return 1

    print(
        f"streaming_tool_call_dedupe_selftest: OK "
        f"(partial_chunks={PARTIAL_CHUNKS}, tool_calls={len(tool_calls)}, "
        f"tool_pendings={len(tool_pendings)}, tool_results={len(tool_results)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
