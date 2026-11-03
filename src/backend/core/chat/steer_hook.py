"""Deliver queued user instructions at neutral tool and context boundaries."""
import asyncio
from core.harness.events import thaw_value
from core.harness.hooks import Decision, DecisionKind, HookSpec, HookStage


def render_steer(messages, *, interrupted):
    """The injected text for one boundary's instructions (one or many)."""
    suffix = ("请立即按这条新指令调整后续计划；不要继续已经被中止的旧工具调用。"
              if interrupted else
              "请立即按这条新指令调整后续计划；上一轮工具结果已经完成，可按需使用。")
    return "[用户在当前执行中追加的新指令]\n" + "\n\n".join(messages) + "\n" + suffix


class SteeringHook:
    """``interrupt_tools=False`` lets issued tool calls run; new input waits for
    the next reasoning boundary. ``take_batch=True`` leases every pending steer
    at once (``take_pending`` then returns a list) and injects them as one item;
    the delivery carries them under ``batch`` so they are acknowledged together.
    """

    def __init__(self, take_pending=None, acknowledge=None, *, interrupt_tools=True,
                 take_batch=False, render=render_steer):
        self.take_pending = take_pending
        self.acknowledge = acknowledge
        self.interrupt_tools = interrupt_tools
        self.take_batch = take_batch
        self.render = render
        self.lock = asyncio.Lock()
        self.delivery = None
        self.interrupted = False

    async def poll(self, run_id):
        if self.delivery is None and run_id:
            if self.take_batch:
                from core.chat.steer import take_pending_steers
                self.delivery = _batch_delivery(await (self.take_pending or take_pending_steers)(run_id))
            else:
                from core.chat.steer import take_pending_steer
                self.delivery = await (self.take_pending or take_pending_steer)(run_id)
        return self.delivery

    async def before_tool(self, invocation):
        if not self.interrupt_tools:
            return Decision()
        async with self.lock:
            if not await self.poll(invocation.run_id):
                return Decision()
            self.interrupted = True
        notice = "用户追加了新指令；本工具调用已在执行前中止，等待模型按新指令重新规划。"
        return Decision(DecisionKind.REJECT, patch={"tool_response": {
            "state": "interrupted", "content": [{"type": "text", "text": notice}]}}, reason=notice)

    async def transform(self, invocation):
        if invocation.operation_name != "agent_reasoning":
            return Decision()
        async with self.lock:
            delivery = await self.poll(invocation.run_id)
            items = (delivery or {}).get("batch") or [delivery or {}]
            messages = [m for m in (str(i.get("message") or "").strip() for i in items) if m]
            if not messages:
                self.delivery = None
                self.interrupted = False
                return Decision()
            item = dict(content=self.render(messages, interrupted=self.interrupted),
                        kind="steer", origin="user:steer", trust="user", priority=950,
                        token_budget=4000, truncation_policy="never")
            return Decision.modify({"context_items": [*invocation.data.get("context_items", ()), item],
                                    "steer_delivery": delivery})

    async def applied(self, invocation):
        delivery = invocation.data.get("steer_delivery")
        if delivery is None:
            return Decision()
        # Called only after the framework has appended the context successfully.
        async with self.lock:
            if self.acknowledge is not None:
                await self.acknowledge(thaw_value(delivery))
            self.delivery = None
            self.interrupted = False
        return Decision()

    def specs(self):
        return (
            HookSpec("user_steering_admission", HookStage.BEFORE_TOOL, self.before_tool,
                     priority=100, timeout_ms=30000, mutable_fields=frozenset({"tool_response"})),
            HookSpec("user_steering_context", HookStage.TRANSFORM_CONTEXT, self.transform,
                     priority=100, timeout_ms=30000,
                     mutable_fields=frozenset({"context_items", "steer_delivery"})),
            HookSpec("user_steering_applied", HookStage.AFTER_CONTEXT, self.applied,
                     priority=100, timeout_ms=30000),
        )


def _batch_delivery(items):
    """One delivery for a leased batch: the first item's identity, every item
    under ``batch``, and the raw messages joined for the persisted user turn."""
    items = [dict(item) for item in (items or []) if item]
    if not items:
        return None
    message = "\n\n".join(m for m in (str(i.get("message") or "").strip() for i in items) if m)
    return {**items[0], "message": message, "batch": items}
