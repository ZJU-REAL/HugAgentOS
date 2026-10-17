"""Deliver user steering at safe model/tool boundaries."""
import asyncio
from agentscope.agent import Agent
from agentscope.message import ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolChunk, ToolResponse
from core.llm.context_adapter import append_context_text, render_text_block
from core.llm.context_ir import KIND_STEER, POLICY_NEVER


class SteerMiddleware(MiddlewareBase):
    """Deliver a queued user instruction at the next safe ReAct boundary.

    ``on_reasoning`` is the primary insertion point: AgentScope calls it after
    the previous tool results have entered context and immediately before the
    next model call. ``on_acting`` is the earlier fallback for a steer that
    arrives while the model is constructing its next tool call; it marks that
    not-yet-started tool batch interrupted so the next reasoning round can
    replan. Both paths preserve a valid assistant-tool-result-user order.
    """

    def __init__(self, take_pending=None, acknowledge=None) -> None:
        self._take_pending = take_pending
        self._acknowledge = acknowledge
        self._lock = asyncio.Lock()
        self._delivery: dict | None = None
        self._interrupted_tools = False

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        run_id = str(getattr(agent.state, "run_id", "") or "")
        if not run_id:
            async for item in next_handler(**input_kwargs):
                yield item
            return

        async with self._lock:
            if self._delivery is None:
                from core.services.chat_steer_service import take_pending_steer

                self._delivery = await (self._take_pending or take_pending_steer)(run_id)
                self._interrupted_tools = self._delivery is not None
            delivery = self._delivery

        if delivery is None:
            async for item in next_handler(**input_kwargs):
                yield item
            return

        notice = "用户追加了新指令；本工具调用已在执行前中止，等待模型按新指令重新规划。"
        block = render_text_block(notice)
        yield ToolChunk(content=[block], state=ToolResultState.INTERRUPTED)
        yield ToolResponse(content=[block], state=ToolResultState.INTERRUPTED)

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        run_id = str(getattr(agent.state, "run_id", "") or "")
        async with self._lock:
            # A steer can arrive while a long-running tool is executing. There
            # is no later on_acting hook in that round, so poll again here,
            # after AgentScope saved the tool result and before it starts the
            # next model call. This is the normal "insert after this tool"
            # path; without it the steer waits until another tool call (or the
            # whole run) finishes.
            if self._delivery is None and run_id:
                from core.services.chat_steer_service import take_pending_steer

                self._delivery = await (self._take_pending or take_pending_steer)(run_id)
            delivery = self._delivery
            interrupted_tools = self._interrupted_tools
            self._delivery = None
            self._interrupted_tools = False

        if delivery is not None:
            message = str(delivery.get("message") or "").strip()
            if message:
                append_context_text(
                    agent,
                    "[用户在当前执行中追加的新指令]\n"
                    f"{message}\n"
                    + (
                        "请立即按这条新指令调整后续计划；不要继续已经被中止的旧工具调用。"
                        if interrupted_tools
                        else "请立即按这条新指令调整后续计划；上一轮工具结果已经完成，可按需使用。"
                    ),
                    kind=KIND_STEER,
                    origin="user:steer",
                    trust="user",
                    priority=950,
                    token_budget=4_000,
                    truncation_policy=POLICY_NEVER,
                )
                if self._acknowledge is not None:
                    await self._acknowledge(delivery)
                agent.state.steer_delivery = dict(delivery)

        async for item in next_handler(**input_kwargs):
            yield item
