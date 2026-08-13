"""自主循环驱动器 v2 行为回归（不跑真实 LLM/沙箱/git，全部桩替换，<1s）。

覆盖本轮 harness 改造的行为契约：
  1. 去预算化：LoopBudget 默认不限，循环跑到账本全通过为止；
  2. 混合验收：check_cmd 由 driver 亲自执行——机检过=免二次复核（收官需求除外），
     机检挂=不烧评审 agent；
  3. per-iteration 异常隔离：worker 抛异常只废本轮（不计 attempt），连续多轮才熔断 failed；
  4. 停滞告警（strategy_change）看 stalls 而非 attempts：健康推进的多轮大需求不再被
     怂恿「换根本不同的方法」；
  5. 评审反馈按需求隔离：翻牌后不泄漏给下一条需求；
  6. blocked 触发重规划（replan_remaining），重拆后继续而非直接「部分完成」收场；
  7. steering：用户运行中追加指令注入下一轮 worker prompt；
  8. 收尾交付轮：部分完成收场时用现有成果跑一轮 wrap-up。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import orchestration.autonomous_loop as al
from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop
from orchestration.loop_evaluator import CONTINUE, DONE, GoalSpec


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class StubPolicy:
    version = "test"
    strategy_change_after = 2
    max_attempts_per_requirement = 3
    budget_multiplier = 1.0


class Harness:
    """把 driver 的全部外部依赖换成可编排的桩，并记录 worker prompt / 评审调用。"""

    def __init__(self, monkeypatch, requirements: List[Dict[str, Any]]):
        self.prompts: List[str] = []
        self.review_calls: List[Dict[str, Any]] = []
        self.review_script: List[Dict[str, Any]] = []
        self.worker_script: List[Any] = []  # dict 结果或 Exception
        self.check_exit: Dict[str, int] = {}  # check_cmd → exit code
        self.worktree_changed = False

        async def fake_worker(**kw):
            self.prompts.append(kw.get("prompt", ""))
            item = self.worker_script.pop(0) if self.worker_script else {
                "text": "干完了", "tokens": 10, "tool_calls": 1}
            if isinstance(item, Exception):
                raise item
            return dict(item)

        async def fake_review(**kw):
            self.review_calls.append({"second_pass": kw.get("second_pass", False),
                                      "requirement_id": kw.get("requirement_id"),
                                      "machine_evidence": kw.get("machine_evidence", "")})
            if self.review_script:
                return dict(self.review_script.pop(0))
            return {"verdict": DONE, "criteria_hit": [], "evidence": "亲读 /workspace/out.md",
                    "progress": True, "feedback": "OK"}

        async def fake_sbx(cmd, **kw):
            for c, code in self.check_exit.items():
                if c in cmd:
                    return (code, "check-output", "")
            return (0, "", "")

        async def fake_scout(**kw):
            return "现状：空工作区"

        async def fake_plan(**kw):
            return [dict(r) for r in requirements]

        async def fake_replan(**kw):
            return None

        async def fake_worktree_changed(*a, **kw):
            return self.worktree_changed

        async def _noop_write(path, content, **kw):
            return None

        async def _noop_read(path, **kw):
            return ""

        async def _criteria(**kw):
            return ["标准1"]

        monkeypatch.setattr(al, "_run_worker_iteration", fake_worker)
        monkeypatch.setattr(al, "review_requirement", fake_review)
        monkeypatch.setattr(al, "_sbx_exec", fake_sbx)
        monkeypatch.setattr(al, "_git_worktree_changed", fake_worktree_changed)
        monkeypatch.setattr(al, "_write_file", _noop_write)
        monkeypatch.setattr(al, "_read_file", _noop_read)
        monkeypatch.setattr(al, "extract_acceptance_criteria", _criteria)
        monkeypatch.setattr(al, "scout_workspace", fake_scout)
        monkeypatch.setattr(al, "plan_requirements", fake_plan)
        monkeypatch.setattr(al, "replan_remaining", fake_replan)
        monkeypatch.setattr(al, "_resolve_loop_policy",
                            lambda **kw: StubPolicy())
        # 退避 sleep 提速
        monkeypatch.setattr(al, "asyncio", SimpleNamespace(
            sleep=self._fast_sleep, gather=asyncio.gather,
            CancelledError=asyncio.CancelledError))
        # ontology 解析不碰 DB
        import core.services.ontology_service as osvc

        monkeypatch.setattr(osvc, "build_user_ontology_runtime",
                            lambda **kw: (False, {"enabled": False, "packs": [],
                                                  "review_level": "none"}))
        self.monkeypatch = monkeypatch

    @staticmethod
    async def _fast_sleep(_s):
        return None

    def go(self, *, budget: LoopBudget = None, poll_steering=None,
           objective: str = "写一个产品页") -> Any:
        return _run(run_autonomous_loop(
            loop_id="t1", user_id="unit",
            goal_spec=GoalSpec(objective=objective, acceptance_criteria=["c1"]),
            budget=budget or LoopBudget(),
            session_id="loop-t1",
            poll_steering=poll_steering,
        ))


@pytest.fixture()
def mk(monkeypatch):
    def _mk(requirements):
        return Harness(monkeypatch, requirements)
    return _mk


# ── 1+2. 去预算化 + 混合验收 ────────────────────────────────────────────────
def test_check_cmd_pass_skips_second_review_except_final(mk):
    """R1 机检过 → 单次评审翻牌（无二次复核）；R2 是收官需求 → 仍要二次复核。"""
    h = mk([
        {"id": "R1", "description": "建骨架", "check_cmd": "CHECK_OK_1"},
        {"id": "R2", "description": "填内容", "check_cmd": "CHECK_OK_2"},
    ])
    h.check_exit = {"CHECK_OK_1": 0, "CHECK_OK_2": 0}
    h.review_script = [
        {"verdict": DONE, "evidence": "e1", "progress": True, "feedback": ""},   # R1 单评审
        {"verdict": DONE, "evidence": "e2", "progress": True, "feedback": ""},   # R2 评审
        {"verdict": DONE, "evidence": "e3", "progress": True, "feedback": ""},   # R2 二次复核
    ]
    res = h.go()
    assert res.status == "completed", res.reason
    r1_calls = [c for c in h.review_calls if c["requirement_id"] == "R1"]
    r2_calls = [c for c in h.review_calls if c["requirement_id"] == "R2"]
    assert len(r1_calls) == 1 and not r1_calls[0]["second_pass"]
    assert len(r2_calls) == 2 and r2_calls[1]["second_pass"]
    # 机检证据传给了评审员
    assert "CHECK_OK_1" in r1_calls[0]["machine_evidence"]


def test_check_cmd_fail_skips_reviewer_entirely(mk):
    """机检挂 → 该轮完全不烧评审 agent，反馈来自命令输出；下一轮机检过再评审。"""
    h = mk([{"id": "R1", "description": "字数达标", "check_cmd": "WC_CHECK"}])
    h.check_exit = {"WC_CHECK": 1}
    h.worktree_changed = True  # 有真实改动 → 算推进，stalls 不涨

    res_holder = {}

    async def run_two_rounds():
        # 第一轮机检挂后把退出码翻成 0，让第二轮通过
        orig_sbx = al._sbx_exec

        async def flip_sbx(cmd, **kw):
            code, out, err = await orig_sbx(cmd, **kw)
            if "WC_CHECK" in cmd and h.check_exit["WC_CHECK"] == 1:
                h.check_exit["WC_CHECK"] = 0  # 下一次就过
                return (1, "还差 2 万字", "")
            return (code, out, err)

        al._sbx_exec = flip_sbx
        h.review_script = [
            {"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""},
            {"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""},
        ]
        res_holder["res"] = await run_autonomous_loop(
            loop_id="t2", user_id="unit",
            goal_spec=GoalSpec(objective="o", acceptance_criteria=[]),
            budget=LoopBudget(), session_id="loop-t2",
        )

    _run(run_two_rounds())
    res = res_holder["res"]
    assert res.status == "completed", res.reason
    # 机检挂的那轮没有任何评审调用：总评审次数 = 第二轮的 评审+二次复核（收官）= 2
    assert len(h.review_calls) == 2
    # 第二轮 worker prompt 带上了机检反馈
    assert any("机检未通过" in p for p in h.prompts[1:])


def test_budget_defaults_unlimited(mk):
    """默认预算不限：需求磨到第 8 轮才过也不会被旧 50 轮/6h 预算拦掉。"""
    h = mk([{"id": "R1", "description": "磨"}])
    h.review_script = (
        [{"verdict": CONTINUE, "evidence": "", "progress": True, "feedback": "再磨"}] * 7
        + [{"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""}] * 2
    )
    res = h.go()
    assert res.status == "completed", (res.status, res.reason)
    assert res.iterations == 8


# ── 3. 异常隔离与熔断 ─────────────────────────────────────────────────────
def test_worker_exception_is_isolated_not_fatal(mk):
    h = mk([{"id": "R1", "description": "x"}])
    h.worker_script = [
        RuntimeError("gateway 500"),
        {"text": "ok", "tokens": 5, "tool_calls": 1},
    ]
    h.review_script = [{"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""}] * 2
    res = h.go()
    assert res.status == "completed", res.reason
    # 异常轮不计 attempt：history 里只有健康轮的评审记录
    assert all(r["verdict"] != "failed" for r in res.history)


def test_consecutive_infra_circuit_breaker(mk, monkeypatch):
    monkeypatch.setenv("LOOP_MAX_CONSECUTIVE_INFRA", "3")
    h = mk([{"id": "R1", "description": "x"}])
    h.worker_script = [RuntimeError("boom")] * 10
    res = h.go()
    assert res.status == "failed"
    assert "熔断" in res.reason


# ── 4. 停滞告警看 stalls ──────────────────────────────────────────────────
def test_strategy_change_follows_stalls_not_attempts(mk):
    """连续 3 轮有实质推进（progress=True）：即便 attempts 超过阈值，也不注入停滞告警。"""
    h = mk([{"id": "R1", "description": "逐章写 20 章"}])
    h.review_script = (
        [{"verdict": CONTINUE, "evidence": "章节+1", "progress": True, "feedback": "继续写"}] * 3
        + [{"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""}] * 2
    )
    res = h.go()
    assert res.status == "completed"
    assert not any("停滞告警" in p for p in h.prompts), "健康推进不应触发换思路告警"


def test_strategy_change_appears_after_stalls(mk):
    h = mk([{"id": "R1", "description": "x"}])
    h.review_script = (
        [{"verdict": CONTINUE, "evidence": "", "progress": False, "feedback": "原地"}] * 2
        + [{"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""}] * 2
    )
    res = h.go()
    assert res.status == "completed"
    # stalls 达到 strategy_change_after(2) 后的那轮 prompt 带停滞告警
    assert any("停滞告警" in p for p in h.prompts)


# ── 5. 反馈按需求隔离 ─────────────────────────────────────────────────────
def test_feedback_not_leaked_across_requirements(mk):
    h = mk([
        {"id": "R1", "description": "a"},
        {"id": "R2", "description": "b"},
    ])
    h.review_script = [
        {"verdict": DONE, "evidence": "e", "progress": True, "feedback": "R1专属反馈XYZ"},
        {"verdict": DONE, "evidence": "e", "progress": True, "feedback": "R1复核反馈XYZ"},
        {"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""},
        {"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""},
    ]
    res = h.go()
    assert res.status == "completed"
    r2_prompt = h.prompts[1]
    assert "XYZ" not in r2_prompt, "上一需求的评审反馈泄漏进了下一需求的 prompt"
    assert "已完成并通过评审" in r2_prompt  # 交接退化为一句完成通告


# ── 6. blocked → 重规划 ──────────────────────────────────────────────────
def test_replan_on_block_then_complete(mk, monkeypatch):
    h = mk([{"id": "R1", "description": "错误方向"}])

    async def fake_replan(**kw):
        return [{"id": "N1", "description": "换个方向", "check_cmd": ""}]

    monkeypatch.setattr(al, "replan_remaining", fake_replan)
    h.review_script = (
        # R1: 3 轮无推进 → stalls 到 max_attempts(3) → blocked → replan
        [{"verdict": CONTINUE, "evidence": "", "progress": False, "feedback": "不对"}] * 3
        # N1: 一次过（评审 + 收官二次复核）
        + [{"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""}] * 2
    )
    res = h.go()
    assert res.status == "completed", (res.status, res.reason)
    assert any("换个方向" in p for p in h.prompts)


# ── 7. steering 注入 ─────────────────────────────────────────────────────
def test_steering_injected_into_next_prompt(mk):
    h = mk([{"id": "R1", "description": "x"}])
    queue = [["改成暗色主题"]]

    def poll():
        return queue.pop(0) if queue else []

    h.review_script = [{"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""}] * 2
    res = h.go(poll_steering=poll)
    assert res.status == "completed"
    assert "改成暗色主题" in h.prompts[0]
    assert "用户临时指令" in h.prompts[0]


# ── 8. 收尾交付轮 ────────────────────────────────────────────────────────
def test_wrapup_runs_on_partial_completion(mk):
    h = mk([
        {"id": "R1", "description": "能做的"},
        {"id": "R2", "description": "做不动的"},
    ])
    h.review_script = (
        [{"verdict": DONE, "evidence": "e", "progress": True, "feedback": ""}] * 2  # R1 评审+二次复核
        + [{"verdict": CONTINUE, "evidence": "", "progress": False, "feedback": "难"}] * 3  # R2 stalls 满
    )
    res = h.go()
    assert res.status == "budget_exhausted"
    assert "收尾交付" in h.prompts[-1], "部分完成收场应有 wrap-up 轮整合交付"


def test_wrapup_skipped_when_nothing_passed(mk):
    h = mk([{"id": "R1", "description": "全程失败"}])
    h.review_script = [
        {"verdict": CONTINUE, "evidence": "", "progress": False, "feedback": "no"}] * 3
    res = h.go()
    assert res.status == "budget_exhausted"
    assert not any("收尾交付" in p for p in h.prompts)
