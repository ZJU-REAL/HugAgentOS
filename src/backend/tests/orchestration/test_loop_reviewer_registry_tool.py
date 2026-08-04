"""Reviewer registry-verification tests.

Artifact registration lives in the backend DB and is invisible to
sandbox-filesystem forensics. The loop reviewer reuses the platform builtin
``builtin.reviewer`` (its existing toolset — no extra tool bindings) and
verifies registration autonomously via the unconditionally-registered
``read_artifact`` tool: a successful read of the worker-reported file_id IS
registry-backed proof.
"""

from core.llm.builtin_subagents import get_builtin_subagent
from orchestration.subagents.loop_reviewer import _build_review_prompt


def test_review_prompt_points_registry_criteria_at_read_artifact():
    p = _build_review_prompt(
        objective="生成报告",
        requirement_desc="交付 docx",
        acceptance_criteria=["已注册为可下载 artifact"],
        worker_summary="我注册好了，file_id=fid_abc",
        second_pass=False,
    )
    assert "read_artifact" in p
    assert "注册表" in p
    # 注册状态由 reviewer 自主调用工具核实，不再有编排层写死的事实注入
    assert "平台侧权威事实" not in p


def test_builtin_reviewer_spec_available():
    # loop_reviewer 复用平台注册的 builtin.reviewer——spec 必须存在且只读
    spec = get_builtin_subagent("builtin.reviewer")
    assert spec is not None
    assert spec.read_only is True
    assert spec.max_iters > 0
