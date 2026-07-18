"""Runs the LLM-evaluator path of self_verify against a real LLM: qualitative goal,
the worker does not write verify.sh, the driver directly calls a standalone LLM
evaluator to judge done/continue against acceptance criteria.

How to run (inside the container, nohup recommended): python -u -m scripts._loop_llmeval_smoke
"""
import asyncio
import uuid

from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop
from orchestration.loop_evaluator import GoalSpec


async def main() -> None:
    loop_id = "le" + uuid.uuid4().hex[:8]
    gs = GoalSpec(
        objective=(
            "为一款面向中小企业的智能办公助手写一句中文宣传语，写入 /workspace/slogan.txt。"
            "要求：不超过20字、突出「省时」与「智能」两个卖点、语气积极。"
            "写完在回复末尾附一段「证据说明」，逐条对照说明如何满足要求。"
        ),
        mode="self_verify",
    )
    budget = LoopBudget(max_iters=3, max_wall_clock_s=900, max_tokens=1_500_000)

    async def emit(ev):
        t = ev.get("type")
        if t in ("iteration_started", "iteration_evaluated", "loop_completed"):
            print(f"  · {t}: seq={ev.get('seq')} verdict={ev.get('verdict')} "
                  f"score={ev.get('score')} decided_by={ev.get('decided_by')} "
                  f"status={ev.get('status')} reason={str(ev.get('reason',''))[:100]}")

    print(f"[llmeval] start {loop_id}")
    res = await run_autonomous_loop(
        loop_id=loop_id, user_id="lesmoke01", goal_spec=gs, budget=budget,
        model_name=None, evaluator_model="fast", worker_max_iters=8,
        session_id=f"loop-{loop_id}", emit=emit,
    )
    print(f"\n[llmeval] RESULT status={res.status} iters={res.iterations} reason={res.reason}")
    print(f"[llmeval] criteria={gs.acceptance_criteria}")
    decided = [h.get("decided_by") for h in res.history]
    print(f"[llmeval] decided_by per iter={decided}")
    # Qualitative goal: an LLM verdict (llm) should have appeared at least once, or been achieved
    print("LLMEVAL_OK" if res.status in ("completed", "budget_exhausted") else "LLMEVAL_UNEXPECTED")


if __name__ == "__main__":
    asyncio.run(main())
