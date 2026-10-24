"""Reasoning policies consume plain snapshots and emit bounded context additions."""
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage


def reminder(text, origin):
    return dict(content=f"<system-reminder>\n{text}\n</system-reminder>",
                kind="reminder", origin=origin, trust="system",
                priority=850, token_budget=4000)


class IterationBudgetHook:
    def __init__(self, threshold=2):
        from core.config.settings import env_bool as _bool, env_value as _env
        self.threshold = threshold
        self.force_text = _bool(_env("CHAT_FINAL_ITER_FORCE_TEXT", "true"))
        self.last_key = None

    def __call__(self, invocation):
        if invocation.operation_name != "agent_reasoning":
            return Decision()
        runtime = invocation.data["runtime"]
        maximum = int(runtime.get("max_iters") or 0)
        current = int(runtime.get("cur_iter") or 0)
        remaining = maximum - current
        if maximum <= self.threshold + 1:
            return Decision()
        patch = {}
        if remaining <= 1 and self.force_text and invocation.data.get("tool_choice") is None:
            patch["tool_choice"] = {"mode": "none"}
        key = (runtime.get("reply_id") or "", current)
        if remaining <= self.threshold and key != self.last_key:
            self.last_key = key
            if remaining <= 1:
                text = ("这是本次回复的最后一轮推理：不要再调用任何工具（再调用会被系统强制"
                        "中断，已完成的工作将无法交付）。请直接基于已获得的信息输出最终回复；"
                        "若任务未全部完成，如实汇报已产出的成果、当前进展和未完成的部分。")
            else:
                text = (f"注意：推理-工具调用轮次即将用尽，包含本轮在内最多还剩 {remaining} 轮。"
                        "请停止新的探索，不要再重试已反复失败的操作，立即整合已获得的信息进行"
                        "收尾；若任务无法在剩余轮次内全部完成，优先输出已有成果与进展说明。")
            patch["context_items"] = [*invocation.data.get("context_items", ()),
                                      reminder(text, "harness:iteration_budget")]
        return Decision.modify(patch) if patch else Decision()


def iteration_budget_hook():
    return HookSpec("iteration_budget", HookStage.TRANSFORM_CONTEXT, IterationBudgetHook(),
                    priority=300, failure_mode=FailureMode.OPEN,
                    mutable_fields=frozenset({"context_items", "tool_choice"}))
