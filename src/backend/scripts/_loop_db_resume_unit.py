"""Deterministic unit check: resume from the DB mirror ledger (phenomenon-1 fix).

Simulates the resume scenario where "the sandbox has been wiped (rebuild/restart) but the DB still has a ledger": all sandbox reads return empty
(_read_ledger->None), and load_ledger returns only a ledger of "R1 passed, R2 not passed, iteration=3".
Asserts the driver **resumes from the DB ledger** (seq starts from 3, no re-decompose to re-split the goal), and reaches completed after R2 passes.
Also verifies save_ledger receives mirror writes throughout. No real LLM/sandbox/git, <1s.

Run: docker exec hugagent-backend python -m scripts._loop_db_resume_unit
"""
import asyncio

import orchestration.autonomous_loop as al
from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop
from orchestration.loop_evaluator import DONE, GoalSpec


async def _fake_worker(**kwargs):
    return {"text": "stub work", "tokens": 10, "tool_calls": 1}


async def _fake_review(**kwargs):
    # R2 review passes directly (with non-empty evidence); the second re-check also goes here -> passed.
    return {"verdict": DONE, "criteria_hit": ["stub"],
            "evidence": "reviewer 读到产出满足需求", "feedback": "满足"}


async def _decompose_must_not_run(**kwargs):
    raise AssertionError("续跑不应重新拆解目标（decompose 被调 = 从0开始的 bug）")


async def _noop_criteria(**kwargs):
    return ["stub 标准"]


async def _sbx_noop(cmd, **kwargs):
    return (0, "", "")


async def _noop_write_file(path, content, **kwargs):
    return None


async def _empty_read_file(path, **kwargs):
    return ""  # sandbox wiped: _read_ledger->None, handoffs empty


async def main() -> None:
    al._run_worker_iteration = _fake_worker
    al.decompose_requirements = _decompose_must_not_run
    al.review_requirement = _fake_review
    al.extract_acceptance_criteria = _noop_criteria
    al._sbx_exec = _sbx_noop
    al._write_file = _noop_write_file
    al._read_file = _empty_read_file

    saved = {"ledgers": []}

    def _save_ledger(led):
        saved["ledgers"].append({r["id"]: r.get("passes") for r in led["requirements"]})

    # DB ledger: R1 passed, R2 pending, already ran to iteration 3 (includes new ledger fields attempts/blocked; also tolerates old fields being ignored).
    db_ledger = {
        "objective": "stub 目标",
        "iteration": 3,
        "criteria": ["stub 标准"],
        "requirements": [
            {"id": "R1", "description": "已完成需求", "passes": True, "evidence": "done",
             "attempts": 2, "blocked": False},
            {"id": "R2", "description": "待完成需求", "passes": False, "evidence": "",
             "attempts": 0, "blocked": False},
        ],
    }

    gs = GoalSpec(objective="stub 目标", acceptance_criteria=["stub"])
    res = await run_autonomous_loop(
        loop_id="dbResume", user_id="unit", goal_spec=gs,
        budget=LoopBudget(max_iters=10, max_wall_clock_s=60, max_tokens=10_000_000),
        session_id="loop-dbResume",
        load_ledger=lambda: db_ledger,
        save_ledger=_save_ledger,
    )
    print(f"[DB-RESUME] status={res.status} iters={res.iterations} reason={res.reason}")
    # Resuming from iteration 3: only 1 more iteration (the 4th) is needed to flip R2 -> completed.
    assert res.status == "completed", res.status
    assert res.iterations == 4, f"应从第3轮续跑、第4轮完成 R2，实际 {res.iterations}"
    assert saved["ledgers"], "save_ledger 应收到镜像写"
    assert saved["ledgers"][-1] == {"R1": True, "R2": True}, saved["ledgers"][-1]
    print("DB_RESUME_UNIT_OK")


if __name__ == "__main__":
    asyncio.run(main())
