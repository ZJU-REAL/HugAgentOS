"""Stall decisions from neutral tool attempts, without framework objects."""
import json
import logging
from core.harness.context_items import reminder_item
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage

logger = logging.getLogger(__name__)


class StallHook:
    def __init__(self, rules):
        self._rules = list(rules or [])
        self._signals = {}
        self._last_call = None
        self._fired = set()

    def __call__(self, invocation):
        if not self._rules:
            return Decision()
        if invocation.stage == HookStage.BEFORE_TOOL:
            signature = (invocation.operation_name,
                         json.dumps(invocation.data.get("raw_input"), sort_keys=True, default=str))
            self._signals["repeated_actions"] = (self._signals.get("repeated_actions", 0) + 1
                                                 if signature == self._last_call else 0)
            self._last_call = signature
            return Decision()
        response = invocation.data.get("tool_result") or {}
        failed = response.get("state") in {"error", "denied", "interrupted"}
        for signal in ("tool_error_streak", "no_progress"):
            self._signals[signal] = self._signals.get(signal, 0) + 1 if failed else 0
        return self.intervene(invocation)

    def intervene(self, invocation):
        from core.evolution.profiles.agent_profile_constants import ACTION_CHANGE_STRATEGY, ACTION_DELEGATE, ACTION_ESCALATE, ACTION_NARROW_SCOPE, ACTION_RETRY, ACTION_ROLLBACK_AND_FORK, ACTION_STOP

        applicable = [
            rule
            for rule in self._rules
            if self._signals.get(getattr(rule, "signal", ""), 0)
            >= int(getattr(rule, "threshold", 0) or 0)
            and int(getattr(rule, "threshold", 0) or 0) > 0
        ]
        if not applicable:
            return
        # Most-specific first: a long stall escalates rather than repeating the
        # mild response that already failed to help.
        rule = max(applicable, key=lambda r: int(getattr(r, "threshold", 0) or 0))
        action = str(getattr(rule, "action", ""))
        signal = str(getattr(rule, "signal", ""))

        # Fire once per (signal, action). Repeating the same reminder every
        # iteration turns it into noise the model learns to skip.
        key = f"{signal}:{action}"
        if key in self._fired:
            return
        self._fired.add(key)

        observed = {
            "repeated_actions": "你已经用完全相同的参数重复调用同一个工具",
            "tool_error_streak": "连续多次工具调用失败",
            "no_progress": "连续多轮没有取得任何可用结果",
        }.get(signal, "当前执行已停滞")

        guidance = {
            ACTION_CHANGE_STRATEGY: "换一条思路：不要重试同样的调用，改用别的工具或别的切入角度。",
            ACTION_NARROW_SCOPE: "缩小范围：先只解决其中最小的一个子问题，拿到确定结果再往下走。",
            ACTION_RETRY: "可以再试一次，但必须改变参数或前置条件，原样重试不会有不同结果。",
            ACTION_DELEGATE: "把这段工作交给合适的子智能体处理，只取回结论。",
            ACTION_ESCALATE: "停止尝试，向用户说明卡在哪里、还缺什么信息。",
            ACTION_STOP: "停止继续尝试，如实汇报已完成的部分和未完成的原因。",
            # Loop-only: a single answer has nothing to roll back to.
            ACTION_ROLLBACK_AND_FORK: "放弃当前路径，回到上一个可用状态重新规划。",
        }.get(action, "")
        if not guidance:
            return

        logger.info(
            "[stall-intervention] %s >= %s -> %s",
            signal,
            getattr(rule, "threshold", 0),
            action,
        )
        message = (
            f"{observed}。{guidance}\n"
            f"（该干预来自当前生效的编排配置：{signal} ≥ "
            f"{getattr(rule, 'threshold', 0)} → {action}）"
        )
        return Decision.modify({
            "context_items": [
                *invocation.data.get("context_items", ()),
                reminder_item(message, "harness:stall_intervention"),
            ],
        })




def stall_hooks(rules):
    policy = StallHook(rules)
    return (HookSpec("stall_attempt", HookStage.BEFORE_TOOL, policy, priority=400,
                     failure_mode=FailureMode.OPEN),
            HookSpec("stall_result", HookStage.AFTER_TOOL, policy, priority=400,
                     failure_mode=FailureMode.OPEN, mutable_fields=frozenset({"context_items"})))
