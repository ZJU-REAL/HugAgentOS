"""Debug tool calling in the AgentScope agent."""
import asyncio
import os
import sys
import logging

logging.disable(logging.CRITICAL)
os.environ["AGENTSCOPE_DISABLE_CONSOLE_OUTPUT"] = "true"


async def main():
    from core.llm.agent_factory import create_agent_executor

    print("Creating agent...", flush=True)
    agent, clients = await create_agent_executor(agent_spec=None, disable_tools=False)

    schemas = agent.toolkit.get_json_schemas()
    print(f"Tools registered: {len(schemas)}", flush=True)
    for s in schemas:
        fn = s.get("function", {})
        props = fn.get("parameters", {}).get("properties", {})
        print(f"  {fn.get('name')}: params={list(props.keys())}", flush=True)

    # Direct tool call
    from agentscope.message._message_block import ToolUseBlock

    tool_call = ToolUseBlock(
        type="tool_use",
        id="test-001",
        name="internet_search",
        input={"query": "AI policy 2024"},
    )
    print(f"\nDirect call: internet_search(query='AI policy 2024')", flush=True)

    try:
        async for chunk in agent.toolkit.call_tool_function(tool_call):
            print(f"  chunk: is_last={chunk.is_last}", flush=True)
            content = chunk.content
            if content:
                print(f"  content_type={type(content).__name__}", flush=True)
                if isinstance(content, list):
                    for item in content[:2]:
                        print(f"    block: {str(item)[:200]}", flush=True)
                else:
                    print(f"    value: {str(content)[:200]}", flush=True)
    except Exception as e:
        import traceback
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()

    # Now test what the agent sees - make it call a tool
    print("\n--- Agent reply with tool call ---", flush=True)
    from agentscope.message import Msg

    agent.set_msg_queue_enabled(True)

    user_msg = Msg(name="user", content="搜索最新AI政策", role="user")

    events = []

    async def consume():
        while True:
            try:
                msg, is_last, _ = await asyncio.wait_for(agent.msg_queue.get(), timeout=60)
                has_tool_use = msg.has_content_blocks("tool_use")
                has_tool_result = msg.has_content_blocks("tool_result")
                has_text = msg.has_content_blocks("text")

                if has_tool_use:
                    for b in msg.get_content_blocks("tool_use"):
                        print(f"  TOOL_USE: name={b.get('name')}, input={str(b.get('input',{}))[:100]}, id={b.get('id')}", flush=True)

                if has_tool_result:
                    for b in msg.get_content_blocks("tool_result"):
                        output = b.get("output", [])
                        output_text = ""
                        if isinstance(output, list):
                            for item in output:
                                if isinstance(item, dict):
                                    output_text += item.get("text", str(item))[:100]
                        print(f"  TOOL_RESULT: name={b.get('name')}, output={output_text[:200]}", flush=True)

                if is_last and msg.role == "assistant" and not has_tool_use:
                    break
            except asyncio.TimeoutError:
                print("  TIMEOUT", flush=True)
                break

    await asyncio.gather(agent.reply(user_msg), consume())
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
