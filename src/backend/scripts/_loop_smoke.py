"""Smoke test: verify create_agent_executor can actually run one round of reply (LLM path) + sandbox bash (worker path).

    docker exec hugagent-backend python /app/src/backend/scripts/_loop_smoke.py
"""
import asyncio
import sys

USER_ID = "copytest_9c0cf31f"


async def test_llm():
    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients
    from orchestration.streaming import StreamingAgent

    agent, clients = await create_agent_executor(
        disable_tools=True,
        enabled_skill_ids=[],
        chat_mode="fast",
        current_user_id=USER_ID,
    )
    sa = StreamingAgent(agent, clients)
    text = ""
    try:
        async for et, payload in sa.stream(
            [{"role": "user", "content": "只回复两个字：收到"}],
            {"user_id": USER_ID, "enable_thinking": False, "chat_mode": "fast"},
        ):
            if et == "text_delta":
                text += payload
            elif et == "error":
                print("LLM_ERROR:", payload)
                return False
    finally:
        await close_clients(clients)
    print(f"LLM_OK text={text!r} usage={sa.get_usage()}")
    return True


async def test_sandbox_bash():
    """Run a bash command with a tools-enabled agent to verify the sandbox is executable."""
    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients
    from orchestration.streaming import StreamingAgent

    sess = f"smoke-{USER_ID}"
    agent, clients = await create_agent_executor(
        current_user_id=USER_ID,
        chat_mode="fast",
        sandbox_session_id=sess,
        isolated=True,
        max_iters=6,
    )
    sa = StreamingAgent(agent, clients)
    saw_tool = False
    text = ""
    try:
        async for et, payload in sa.stream(
            [{"role": "user", "content": "用 bash 执行 `echo LOOP_SANDBOX_OK` 并把输出原样告诉我。"}],
            {"user_id": USER_ID, "enable_thinking": False, "chat_mode": "fast"},
        ):
            if et == "tool_call":
                saw_tool = True
                print("TOOL_CALL:", payload.get("name"))
            elif et == "tool_result":
                print("TOOL_RESULT:", str(payload.get("content"))[:200])
            elif et == "text_delta":
                text += payload
            elif et == "error":
                print("SANDBOX_ERROR:", payload)
    finally:
        await close_clients(clients)
    ok = "LOOP_SANDBOX_OK" in text or saw_tool
    print(f"SANDBOX_{'OK' if ok else 'FAIL'} saw_tool={saw_tool} text={text[:120]!r}")
    return ok


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "llm"
    if which in ("llm", "all"):
        if not await test_llm():
            print("SMOKE_FAIL: llm")
            return
    if which in ("sandbox", "all"):
        await test_sandbox_bash()
    print("SMOKE_DONE")


if __name__ == "__main__":
    asyncio.run(main())
