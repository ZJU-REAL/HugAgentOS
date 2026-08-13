"""Deterministic unit check: exit paths of the requirement ledger + read-only review sub-agent.

驱动器 v2（去预算化 + 规划器侦察/重规划 + 混合验收）后的收敛冒烟：不跑真实
LLM/沙箱/git，桩掉 worker/规划/评审/沙箱，验证三条出口：
  A. 全部需求通过 → completed（无机检需求翻牌仍需二次复核）；
  B. 单需求连续无推进 → stalls 到停滞上限 → blocked →（重规划桩返回 None）→
     budget_exhausted 部分完成；
  C. done 被二次复核驳回 → 不翻牌，直至 blocked。
完成 <1s。Run: docker exec hugagent-backend python -m scripts._loop_convergence_unit
（正式回归见 tests/orchestration/test_autonomous_loop_driver.py）
"""
import asyncio

import orchestration.autonomous_loop as al
from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop
from orchestration.loop_evaluator import CONTINUE, DONE, GoalSpec


class _StubPolicy:
    version = "unit"
    strategy_change_after = 2
    max_attempts_per_requirement = 6
    budget_multiplier = 1.0


async def _fake_worker(**kwargs):
    return {"text": "stub work", "tokens": 10, "tool_calls": 1}


def _make_fake_review(verdicts):
    """按调用顺序回放判定；done 带非空证据（否则 driver 会降级为 continue）。"""
    calls = {"i": 0}

    async def _fake_review(**kwargs):
        i = calls["i"]
        calls["i"] += 1
        v = verdicts[min(i, len(verdicts) - 1)]
        return {"verdict": v, "criteria_hit": ["stub"],
                "evidence": "reviewer 读到 /proj/index.html 含目标内容" if v == DONE else "",
                "progress": False,
                "feedback": "stub 反馈"}

    return _fake_review, calls


def _fake_plan_n(n):
    async def _fake(**kwargs):
        return [{"id": f"R{i}", "description": f"stub 需求{i}"} for i in range(1, n + 1)]
    return _fake


async def _noop_criteria(**kwargs):
    return ["stub 标准"]


async def _sbx_noop(cmd, **kwargs):
    return (0, "", "")


async def _noop_write_file(path, content, **kwargs):
    return None


async def _noop_read_file(path, **kwargs):
    return ""  # → _read_ledger returns None → fresh init every time


async def _noop_scout(**kwargs):
    return ""


async def _noop_replan(**kwargs):
    return None


async def _worktree_clean(*a, **kwargs):
    return False


def _patch_common():
    al._run_worker_iteration = _fake_worker
    al._sbx_exec = _sbx_noop
    al._write_file = _noop_write_file
    al._read_file = _noop_read_file
    al._git_worktree_changed = _worktree_clean
    al.extract_acceptance_criteria = _noop_criteria
    al.scout_workspace = _noop_scout
    al.replan_remaining = _noop_replan
    al._resolve_loop_policy = lambda **kw: _StubPolicy()

    import core.services.ontology_service as osvc

    osvc.build_user_ontology_runtime = lambda **kw: (
        False, {"enabled": False, "packs": [], "review_level": "none"})


_MAX_ATTEMPTS = _StubPolicy.max_attempts_per_requirement


async def main() -> None:
    _patch_common()

    # ── Scenario A: 3 需求，每条第 2 轮 done 且二次复核通过 → completed。
    al.plan_requirements = _fake_plan_n(3)
    seq = []
    for _ in range(3):
        seq += [CONTINUE, DONE, DONE]  # done 后紧跟二次复核的 done
    al.review_requirement, _ = _make_fake_review(seq)
    resA = await run_autonomous_loop(
        loop_id="convA", user_id="unit", goal_spec=GoalSpec(objective="stub", acceptance_criteria=["c"]),
        budget=LoopBudget(),  # 默认不限预算
        session_id="loop-convA",
    )
    print(f"[A] status={resA.status} iters={resA.iterations} final={resA.final_score} reason={resA.reason}")
    assert resA.status == "completed", resA.status
    assert resA.final_score == 1.0, resA.final_score

    # ── Scenario B: 1 需求永远 continue（无推进）→ stalls 到上限 → blocked → budget_exhausted。
    al.plan_requirements = _fake_plan_n(1)
    al.review_requirement, _ = _make_fake_review([CONTINUE])
    resB = await run_autonomous_loop(
        loop_id="convB", user_id="unit", goal_spec=GoalSpec(objective="stub2", acceptance_criteria=["c"]),
        budget=LoopBudget(),
        session_id="loop-convB",
    )
    print(f"[B] status={resB.status} iters={resB.iterations} final={resB.final_score}")
    assert resB.status == "budget_exhausted", resB.status
    assert resB.iterations == _MAX_ATTEMPTS, resB.iterations
    assert resB.final_score == 0.0, resB.final_score

    # ── Scenario C: done 被二次复核驳回 → 不翻牌，直至 blocked。
    al.plan_requirements = _fake_plan_n(1)
    al.review_requirement, _ = _make_fake_review([DONE, CONTINUE])
    resC = await run_autonomous_loop(
        loop_id="convC", user_id="unit", goal_spec=GoalSpec(objective="stub3", acceptance_criteria=["c"]),
        budget=LoopBudget(),
        session_id="loop-convC",
    )
    print(f"[C] status={resC.status} iters={resC.iterations}")
    assert resC.status == "budget_exhausted", "done 被二次复核驳回不应翻牌"
    assert resC.iterations == _MAX_ATTEMPTS, resC.iterations

    print("CONVERGENCE_UNIT_OK")


if __name__ == "__main__":
    asyncio.run(main())
