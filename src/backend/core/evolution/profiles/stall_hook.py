"""Stall decisions from neutral tool attempts, without framework objects.

Progress is judged by what a call produced, not by whether it succeeded: a
command that exits 0 and prints the same thing as before has told the model
nothing new. Each result is reduced to a fingerprint (volatile identifiers and
timings removed) and compared with the recent window of results.

Signals, counted per tool result:

* ``no_progress`` — consecutive results that failed or were already seen in the
  window; a new successful result resets it.
* ``repeated_actions`` — consecutive calls that repeat an input from the window
  and also got nothing new back. Polling a status that changes is not counted.
* ``tool_error_streak`` — consecutive failed results.

A reminder states the facts (how many calls, which tool, what it keeps
returning). It fires when a rule's threshold is reached and again each time
the streak doubles, so a long stall is told about again with current numbers
instead of once at the start; the schedule restarts when the streak resets.
"""
import hashlib
import json
import logging
import re
from collections import deque

from core.harness.context_items import reminder_item
from core.harness.events import thaw_value
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage

logger = logging.getLogger(__name__)

WINDOW = 20
PREVIEW_CHARS = 120
FAILED_STATES = {"error", "denied", "interrupted"}
# Keys whose values change on every call without saying anything about the outcome.
_VOLATILE_KEY = re.compile(r"(^id$|_id$|_ms$|_at$|^timestamp$|^lifetime_note$|cursor)")
_VOLATILE_TEXT = (
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"\b(?:[a-z]+_)?[0-9a-f]{16,}\b", re.I),
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
)


def _result_text(response):
    from core.llm.runtime.message_compat import flatten_tool_output
    return flatten_tool_output(response.get("content")) if isinstance(response, dict) else ""


def _strip_volatile(value):
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if not _VOLATILE_KEY.search(str(k))}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def _normalized(text):
    try:
        text = json.dumps(_strip_volatile(json.loads(text)), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        pass
    for pattern in _VOLATILE_TEXT:
        text = pattern.sub("#", text)
    return " ".join(text.split())


def _fingerprint(*parts):
    return hashlib.sha256("\x00".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def _preview(text):
    """What a person would call the output: a command's stdout or error, else the text itself."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("stdout", "error", "stderr", "message"):
            if str(data.get(key) or "").strip():
                text = str(data[key])
                break
    text = " ".join(str(text).split())
    if not text:
        return "（空）"
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


class StallHook:
    def __init__(self, rules):
        self._rules = list(rules or [])
        self._signals = {}
        self._pending = {}
        self._unnamed = deque()
        self._inputs = deque(maxlen=WINDOW)
        self._results = deque(maxlen=WINDOW)
        # Calls in the current no-progress streak: (tool, preview, failed).
        self._streak = []
        # (signal, action) -> streak length at which it next fires.
        self._next_fire = {}

    def __call__(self, invocation):
        if not self._rules:
            return Decision()
        call_id = str((invocation.data.get("metadata") or {}).get("tool_call_id") or "")
        if invocation.stage == HookStage.BEFORE_TOOL:
            signature = _fingerprint(invocation.operation_name, _normalized(json.dumps(
                thaw_value(invocation.data.get("raw_input")), sort_keys=True, default=str, ensure_ascii=False)))
            if call_id:
                self._pending[call_id] = signature
            else:
                self._unnamed.append(signature)
            return Decision()
        signature = self._pending.pop(call_id, None) if call_id else None
        if signature is None and self._unnamed:
            signature = self._unnamed.popleft()
        response = thaw_value(invocation.data.get("tool_result"))
        self.observe(invocation.operation_name, signature, response if isinstance(response, dict) else {})
        return self.intervene(invocation)

    def observe(self, tool, signature, response):
        text = _result_text(response)
        failed = response.get("state") in FAILED_STATES
        result = _fingerprint(tool, _normalized(text))
        seen_result = result in self._results
        seen_input = signature is not None and signature in self._inputs
        stale = failed or seen_result

        self._bump("tool_error_streak", failed)
        self._bump("no_progress", stale)
        self._bump("repeated_actions", seen_input and stale)
        self._streak = [*self._streak, (tool, _preview(text), failed)][-WINDOW:] if stale else []

        self._results.append(result)
        if signature is not None:
            self._inputs.append(signature)

    def _bump(self, signal, hit):
        self._signals[signal] = self._signals.get(signal, 0) + 1 if hit else 0
        if not hit:
            for key in [key for key in self._next_fire if key[0] == signal]:
                del self._next_fire[key]

    def intervene(self, invocation):
        from core.evolution.profiles.agent_profile_constants import ACTION_CHANGE_STRATEGY, ACTION_DELEGATE, ACTION_ESCALATE, ACTION_NARROW_SCOPE, ACTION_RETRY, ACTION_ROLLBACK_AND_FORK, ACTION_STOP

        applicable = [
            rule
            for rule in self._rules
            if int(getattr(rule, "threshold", 0) or 0) > 0
            and self._signals.get(getattr(rule, "signal", ""), 0)
            >= int(getattr(rule, "threshold", 0) or 0)
        ]
        if not applicable:
            return
        # Most-specific first: a long stall escalates rather than repeating the
        # mild response that already failed to help.
        rule = max(applicable, key=lambda r: int(getattr(r, "threshold", 0) or 0))
        action = str(getattr(rule, "action", ""))
        signal = str(getattr(rule, "signal", ""))
        threshold = int(getattr(rule, "threshold", 0) or 0)
        count = self._signals.get(signal, 0)

        # Once at the threshold, then each time the streak doubles: a stall that
        # goes on is told about again with current numbers, without a reminder
        # on every call.
        key = (signal, action)
        if count < self._next_fire.get(key, threshold):
            return
        self._next_fire[key] = count * 2

        guidance = {
            ACTION_CHANGE_STRATEGY: "换一条思路：不要再重复这些调用，改用别的工具或别的切入角度。",
            ACTION_NARROW_SCOPE: "缩小范围：只保留能确定完成的部分，不要在同一处反复修改。",
            ACTION_RETRY: "可以再试一次，但必须改变参数或前置条件，原样重试不会有不同结果。",
            ACTION_DELEGATE: "把这段工作交给合适的子智能体处理，只取回结论。",
            ACTION_ESCALATE: "停止尝试，向用户说明卡在哪里、还缺什么信息。",
            ACTION_STOP: "停止继续尝试，如实汇报已完成的部分和未完成的原因。",
            # Loop-only: a single answer has nothing to roll back to.
            ACTION_ROLLBACK_AND_FORK: "放弃当前路径，回到上一个可用状态重新规划。",
        }.get(action, "")
        if not guidance:
            return

        logger.info("[stall-intervention] %s=%s (threshold %s) -> %s", signal, count, threshold, action)
        message = f"{self.facts(signal, count)}\n{guidance}"
        if signal in ("no_progress", "repeated_actions"):
            message += "\n如果已有内容基本可用，现在就直接交付，并说明哪项要求没有满足。"
        return Decision.modify({
            "context_items": [
                *invocation.data.get("context_items", ()),
                reminder_item(message, "harness:stall_intervention"),
            ],
        })

    def facts(self, signal, count):
        """What the model has been doing, in its own tools' terms."""
        streak = self._streak[-count:] if count else []
        if signal == "tool_error_streak":
            tool, preview, _ = streak[-1] if streak else ("", "", True)
            return f"最近 {count} 次工具调用都失败了，最后一次是 {tool}：{preview}"
        outcomes = []
        for tool, preview, failed in streak:
            outcome = (tool, ("失败：" if failed else "") + preview)
            if outcome not in outcomes:
                outcomes.append(outcome)
        if signal == "repeated_actions":
            head = f"最近 {count} 次调用都在重复之前做过的操作，结果也和之前一样"
        else:
            head = f"最近 {count} 次工具调用都没有产生新结果"
        if len(outcomes) == 1:
            tool, preview = outcomes[0]
            return f"{head}：{tool} 的输出始终是 `{preview}`。重复同样的操作不会得到不同结果。"
        lines = "\n".join(f"- {tool}：`{preview}`" for tool, preview in outcomes[:3])
        return f"{head}，反复得到的是：\n{lines}\n重复同样的操作不会得到不同结果。"


def stall_hooks(rules):
    policy = StallHook(rules)
    return (HookSpec("stall_attempt", HookStage.BEFORE_TOOL, policy, priority=400,
                     failure_mode=FailureMode.OPEN),
            HookSpec("stall_result", HookStage.AFTER_TOOL, policy, priority=400,
                     failure_mode=FailureMode.OPEN, mutable_fields=frozenset({"context_items"})))
