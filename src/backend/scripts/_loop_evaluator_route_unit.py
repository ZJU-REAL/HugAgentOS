"""Deterministic unit check of self_verify's auto-routing: verify.sh exists -> script (rule) decides; absent -> LLM evaluator decides.

Stubs out the sandbox and LLM, only verifying evaluate_iteration's routing and verdict. <1s, does not touch real infrastructure.
Run: docker exec hugagent-backend python -m scripts._loop_evaluator_route_unit
"""
import asyncio

import orchestration.loop_evaluator as le
from orchestration.loop_evaluator import (
    CONTINUE, DONE, GoalSpec, VerifyResult, evaluate_iteration,
)


async def main() -> None:
    # ── Scenario 1: verify.sh exists -> script path (rule); self_verify with no threshold does not early-stop on a valid solution ──
    async def _exists_true(**kw):
        return True

    async def _fake_run_verify(cmd, **kw):
        return VerifyResult(ran=True, exit_code=0, stdout="SCORE=88.0", stderr="", score=None)

    le._verify_script_exists = _exists_true
    le._run_verify = _fake_run_verify
    # Disable the LLM feedback call to avoid hitting the real model
    async def _no_fb(**kw):
        return None
    le._llm_feedback = _no_fb

    gs = GoalSpec(objective="优化X", mode="self_verify")  # target None
    v1 = await evaluate_iteration(goal_spec=gs, session_id="s", user_id="u",
                                  iteration_summary="w", model_name="fast")
    print(f"[1 script] verdict={v1['verdict']} score={v1['score']} decided_by={v1['decided_by']} exit={v1['verify_exit']}")
    assert v1["verdict"] == CONTINUE, v1  # valid solution (exit0) but no threshold -> no early stop
    assert v1["score"] == 88.0
    assert v1["decided_by"] == "environment"
    assert v1["verify_exit"] == 0

    # ── Scenario 2: verify.sh absent -> LLM evaluator decides done ──
    async def _exists_false(**kw):
        return False

    async def _fake_judge_done(prompt, **kw):
        return '{"verdict":"done","criteria_hit":["满足A"],"evidence":"...","reason":"全部满足"}'

    le._verify_script_exists = _exists_false
    le._judge_once = _fake_judge_done
    gs2 = GoalSpec(objective="写一篇文案", mode="self_verify",
                   acceptance_criteria=["语气正式", "含要点A", "无错别字"])
    v2 = await evaluate_iteration(goal_spec=gs2, session_id="s", user_id="u",
                                  iteration_summary="成品+证据", model_name="fast")
    print(f"[2 llm-done] verdict={v2['verdict']} decided_by={v2['decided_by']}")
    assert v2["verdict"] == DONE, v2
    assert v2["decided_by"] == "llm"

    # ── Scenario 3: LLM evaluator decides continue (insufficient evidence) ──
    async def _fake_judge_continue(prompt, **kw):
        return '{"verdict":"continue","reason":"要点A未覆盖"}'

    le._judge_once = _fake_judge_continue
    v3 = await evaluate_iteration(goal_spec=gs2, session_id="s", user_id="u",
                                  iteration_summary="半成品", model_name="fast")
    print(f"[3 llm-continue] verdict={v3['verdict']} decided_by={v3['decided_by']}")
    assert v3["verdict"] == CONTINUE, v3
    assert v3["decided_by"] == "llm"

    # ── Scenario 4: self_verify with no script and no criteria -> lazily extract acceptance criteria (stub returns a non-array -> fall back to [objective])
    #    then LLM decides; here judge still returns continue, verifying the safety property "never falsely judge done". ──
    gs4 = GoalSpec(objective="模糊目标", mode="self_verify", acceptance_criteria=[])
    v4 = await evaluate_iteration(goal_spec=gs4, session_id="s", user_id="u",
                                  iteration_summary="x", model_name="fast")
    print(f"[4 lazy-criteria] verdict={v4['verdict']} decided_by={v4['decided_by']}")
    assert v4["verdict"] == CONTINUE, v4
    assert gs4.acceptance_criteria == ["模糊目标"], gs4.acceptance_criteria  # lazy fallback written back

    # ── Scenario 5: acceptance-criteria extraction parsing ──
    async def _fake_judge_criteria(prompt, **kw):
        return '这是标准：\n["语气正式","覆盖要点A","结尾有行动号召"]'

    le._judge_once = _fake_judge_criteria
    crit = await le.extract_acceptance_criteria(objective="写文案", model_name="fast", user_id="u")
    print(f"[5 extract] criteria={crit}")
    assert crit == ["语气正式", "覆盖要点A", "结尾有行动号召"], crit

    print("EVALUATOR_ROUTE_UNIT_OK")


if __name__ == "__main__":
    asyncio.run(main())
