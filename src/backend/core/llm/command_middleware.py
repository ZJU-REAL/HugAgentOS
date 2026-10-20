"""Insert terminal command facts only between complete ReAct steps."""
import asyncio
from agentscope.middleware import MiddlewareBase
from core.llm.context_adapter import append_context_text
from core.llm.context_ir import KIND_REMINDER, POLICY_NEVER
from core.services.commands import delivery
from core.services.commands import store

class CommandCompletionMiddleware(MiddlewareBase):
    async def on_reasoning(self, agent, input_kwargs, next_handler):
        state = agent.state
        run_id = str(state.run_id or "")
        rows = []
        if (run_id and state.chat_id and state.user_id
                and await asyncio.to_thread(store.has_pending, state.chat_id, state.user_id)):
            rows = await asyncio.to_thread(delivery.reserve_boundary,
                state.chat_id, state.user_id, run_id)
        if rows:
            append_context_text(agent, delivery.notice_text(rows), kind=KIND_REMINDER,
                origin="harness:command_completion", trust="system", priority=950,
                token_budget=4000, truncation_policy=POLICY_NEVER)
        async for item in next_handler(**input_kwargs):
            yield item

    async def on_reply(self, agent, input_kwargs, next_handler):
        if not (agent.state.run_id and agent.state.chat_id and agent.state.user_id):
            async for item in next_handler(**input_kwargs):
                yield item
            return
        current = input_kwargs
        while True:
            async for item in next_handler(**current):
                yield item
            # Close the final-response race: completions arriving during the last
            # output receive a new reasoning step in the same active run.
            rows = await asyncio.to_thread(store.pending_notices, agent.state.chat_id)
            if not any(r["notice_status"] == "pending" and r["user_id"] == agent.state.user_id for r in rows):
                return
            current = {**input_kwargs, "inputs": None}
