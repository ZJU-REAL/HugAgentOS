"""Workspace and plan reminders operate on plain context snapshots."""
import json
from collections.abc import Mapping
from core.harness.events import thaw_value
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage
from core.llm.context.reasoning_hooks import reminder
from core.llm.runtime.hooks_constants import FILE_ID_RE, PIN_HINT_SKIP_TOOLS
from core.llm.runtime.message_compat import flatten_tool_output
from core.llm.tools.plan_update import parse_plan_update_args


def content_blocks(context, kind):
    for message in context:
        if not isinstance(message, Mapping):
            continue
        for block in message.get("content") or ():
            if isinstance(block, Mapping) and block.get("type") == kind:
                yield block


def append_reminder(invocation, text, origin):
    return Decision.modify({"context_items": [*invocation.data.get("context_items", ()),
                                              reminder(text, origin)]})


class WorkspacePinHook:
    def __init__(self):
        self.seen = set()
        self.last_signature = ""

    def reset(self, invocation):
        self.seen.clear()
        self.last_signature = ""
        return Decision()

    def finish(self, invocation):
        runtime = invocation.data.get("runtime", {})
        if runtime.get("finish_pins_completed"):
            return Decision()
        remaining = sorted(self.seen - set(runtime.get("pinned_file_ids") or ()))
        return Decision.modify({"pin_file_ids": remaining}) if remaining else Decision()

    def __call__(self, invocation):
        if invocation.operation_name != "agent_reasoning":
            return Decision()
        context = list(invocation.data.get("context") or ())
        for block in content_blocks(context[-12:], "tool_result"):
            if block.get("name", "") in PIN_HINT_SKIP_TOOLS:
                continue
            text = flatten_tool_output(block.get("output"))
            if '"file_id"' in text:
                self.seen.update(FILE_ID_RE.findall(text))
        unpinned = self.seen - set(invocation.data["runtime"].get("pinned_file_ids") or ())
        signature = ",".join(sorted(unpinned))
        if not unpinned or signature == self.last_signature:
            return Decision()
        preview = sorted(unpinned)[:6]
        rendered = ", ".join(preview)
        if len(unpinned) > len(preview):
            rendered += f", …(+{len(unpinned) - len(preview)} 个)"
        self.last_signature = signature
        return append_reminder(invocation,
            f"沙盒里有 {len(unpinned)} 个 file_id 还没 pin：[{rendered}]。"
            "若是给用户的最终产物，必须调 `pin_to_workspace(file_ids=[...])` 才能交付。",
            "harness:workspace_pin")


class PlanStaleHook:
    def __init__(self, interval=10):
        self.interval = max(1, int(interval))
        self.since_last = 0
        self.fingerprint = None

    def __call__(self, invocation):
        if invocation.operation_name != "agent_reasoning":
            return Decision()
        blocks = list(content_blocks(invocation.data.get("context") or (), "tool_call"))
        plan = None
        for block in reversed(blocks):
            if block.get("name") != "update_plan":
                continue
            raw = block.get("input")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (ValueError, TypeError):
                    raw = None
            plan = parse_plan_update_args(thaw_value(raw))
            fingerprint = str(block.get("id") or "") or json.dumps(plan or {}, ensure_ascii=False, sort_keys=True)
            break
        if not plan:
            return Decision()
        if fingerprint != self.fingerprint:
            self.fingerprint, self.since_last = fingerprint, 0
            return Decision()
        steps = plan.get("steps") or []
        if not any(step.get("status") in ("pending", "in_progress") for step in steps):
            return Decision()
        self.since_last += 1
        if self.since_last < self.interval:
            return Decision()
        done = sum(step.get("status") == "completed" for step in steps)
        checklist = "\n".join(f"  {i}. [{step.get('status', 'pending')}] {step.get('title', '')}"
                              for i, step in enumerate(steps, 1))
        text = PLAN_REMINDER.format(done=done, total=len(steps), rounds=self.since_last, checklist=checklist)
        self.since_last = 0
        return append_reminder(invocation, text, "harness:plan_stale")


def workspace_pin_hooks(*, allow_finish=False):
    policy = WorkspacePinHook()
    specs = (HookSpec("workspace_pin_reset", HookStage.BEFORE_RUN, policy.reset, failure_mode=FailureMode.OPEN),
            HookSpec("workspace_pin", HookStage.TRANSFORM_CONTEXT, policy, priority=200,
                     failure_mode=FailureMode.OPEN, mutable_fields=frozenset({"context_items"})))
    if allow_finish:
        specs += (HookSpec("finish_pin_guard", HookStage.BEFORE_FINISH, policy.finish,
                           failure_mode=FailureMode.OPEN, mutable_fields=frozenset({"pin_file_ids"})),)
    return specs


def plan_stale_hook():
    return HookSpec("plan_stale", HookStage.TRANSFORM_CONTEXT, PlanStaleHook(), priority=600,
                    failure_mode=FailureMode.OPEN, mutable_fields=frozenset({"context_items"}))

PLAN_REMINDER = '`update_plan` 已经 {rounds} 轮没有用过了。当前计划栏停在 {done}/{total}：\n{checklist}\n\n如果其中有步骤其实已经做完，可以调用一次 `update_plan` 把状态补上（传全量列表）；如果这份\n清单已经不符合你正在做的事，重写它比让它停在原地更好。仅在确实相关时才使用——这只是一条\n温和的提醒，不适用就忽略，不要为了回应它而打断手上的工作。不要向用户提起这条提醒。'
