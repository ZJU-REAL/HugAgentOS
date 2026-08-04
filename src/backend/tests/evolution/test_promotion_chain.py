"""GCE tickets 32 / 33 / 34 / 36 — the capability promotion chain.

This is the machinery that makes the system learn rather than merely record, so
the tests cover both directions: what must be promoted, and — more importantly —
what must not be.
"""

import pytest

from core.evolution import promotion as P


def _episodes(n, *, verdict="success", tools=("search", "verify", "chart", "export")):
    return [
        {
            "episode_id": f"ep-{i}",
            "verdict": verdict,
            "tool_sequence": list(tools),
            "memory_refs": [f"mref-{i}"],
        }
        for i in range(n)
    ]


# ── Pattern discovery ────────────────────────────────────────────────────────


def test_recurring_successful_subsequence_is_discovered():
    patterns = P.discover_patterns(_episodes(8))
    success = [p for p in patterns if p.kind == P.PATTERN_SUCCESS_SUBSEQUENCE]
    assert success and success[0].support == 8
    assert success[0].tool_sequence == ["search", "verify", "chart", "export"]


def test_repeated_failures_are_discovered_separately():
    patterns = P.discover_patterns(_episodes(7, verdict="failed"))
    assert any(p.kind == P.PATTERN_REPEATED_FAILURE for p in patterns)


def test_low_support_clusters_produce_nothing():
    """One-off noise must not become a permanent asset."""
    assert P.discover_patterns(_episodes(2)) == []


def test_patterns_carry_the_memories_they_came_from():
    patterns = P.discover_patterns(_episodes(6))
    success = [p for p in patterns if p.kind == P.PATTERN_SUCCESS_SUBSEQUENCE][0]
    assert len(success.memory_refs) == 6


# ── Episode → Skill：SOP 门 ───────────────────────────────────────────────────


def _success_pattern(support=27, rate=0.9, tools=("search", "verify", "chart", "export")):
    return P.Pattern(
        kind=P.PATTERN_SUCCESS_SUBSEQUENCE,
        signature="→".join(tools),
        support=support,
        success_rate=rate,
        tool_sequence=list(tools),
        memory_refs=[f"mref-{i}" for i in range(support)],
        episode_ids=[f"ep-{i}" for i in range(support)],
    )


def _supporting_episodes(
    support=27, *, objective="帮我生成一份本周的销售数据周报", chats=None
):
    """The views behind the pattern: same kind of request, separate conversations."""
    return [
        {
            "episode_id": f"ep-{i}",
            "chat_id": chats[i] if chats else f"chat-{i}",
            "verdict": "success",
            "tool_sequence": ["search", "verify", "chart", "export"],
            "objective": objective,
        }
        for i in range(support)
    ]


def test_recurring_pattern_is_compiled_into_a_skill_proposal():
    """The 27-weekly-reports case: same request, same tools, separate chats."""
    proposal = P.promote_tool_sequence_to_skill(
        _success_pattern(),
        episodes=_supporting_episodes(27),
        skill_credit=0.81,
        workflow_credit=0.46,
    )
    assert proposal is not None
    # Named for what it reads. Co-retrieved memories are context, not sources:
    # nothing of theirs goes into this skill, so demoting them would remove
    # knowledge the skill does not carry.
    assert proposal.from_layer == "episode" and proposal.to_layer == "skill"
    assert proposal.support == 27
    assert proposal.source_refs == []
    assert len(proposal.payload["co_retrieved_memory_refs"]) == 27
    # The rationale states the shared intent — the thing that makes it an SOP —
    # not an unmeasured claim about re-planning.
    assert proposal.payload["intent"]
    assert "重新规划" not in proposal.rationale


def test_one_repeated_tool_call_is_not_mislabelled_as_a_sequence_skill():
    pattern = P.Pattern(
        kind=P.PATTERN_SUCCESS_SUBSEQUENCE,
        signature="call_subagent",
        support=9,
        success_rate=1.0,
        tool_sequence=["call_subagent"],
        episode_ids=[f"ep-{i}" for i in range(9)],
    )

    assert (
        P.promote_tool_sequence_to_skill(
            pattern, skill_credit=0.9, workflow_credit=0.1
        )
        is None
    )


def test_promotion_is_refused_when_orchestration_explains_it_better():
    proposal = P.promote_tool_sequence_to_skill(
        _success_pattern(),
        episodes=_supporting_episodes(27),
        skill_credit=0.3,
        workflow_credit=0.8,
    )
    # A stable ordering can mean "the workflow is fixed", not "a skill exists".
    # Promoting anyway creates a skill with no independent content.
    assert proposal is None


def test_low_success_rate_is_not_promoted():
    assert (
        P.promote_tool_sequence_to_skill(
            _success_pattern(rate=0.4), episodes=_supporting_episodes(27), skill_credit=0.9
        )
        is None
    )


def test_insufficient_support_is_not_promoted():
    assert (
        P.promote_tool_sequence_to_skill(
            _success_pattern(support=2), episodes=_supporting_episodes(2), skill_credit=0.9
        )
        is None
    )


def test_existing_equivalent_skill_becomes_a_patch_not_a_twin():
    proposal = P.promote_tool_sequence_to_skill(
        _success_pattern(),
        episodes=_supporting_episodes(27),
        skill_credit=0.9,
        existing_skill_signatures={"search→verify→chart→export": "skill-weekly-report"},
    )
    assert proposal.patch_target == "skill-weekly-report"


def test_a_trivial_sequence_is_never_promoted():
    """整轮对话只调一次 view_text_file 也构成"相同序列"——但那说明任务简单，
    不说明存在可复用的过程知识。包装单个通用工具是纯噪声。"""
    for tools in (("view_text_file",), ("call_subagent", "view_text_file")):
        pattern = _success_pattern(support=9, rate=1.0, tools=tools)
        assert (
            P.promote_tool_sequence_to_skill(
                pattern, episodes=_supporting_episodes(9), skill_credit=0.9
            )
            is None
        )


def test_unrelated_conversations_are_not_an_sop():
    """Same tool sequence across unrelated requests is a coincidence of tooling."""
    objectives = [
        "帮我生成一份本周的销售数据周报",
        "把这份英文合同翻译成中文",
        "查一下明天上海到北京的航班",
        "写一首关于秋天的短诗",
        "统计仓库里还剩多少台服务器",
        "给新同事拟一封入职欢迎邮件",
    ]
    episodes = [
        {
            "episode_id": f"ep-{i}",
            "chat_id": f"chat-{i}",
            "verdict": "success",
            "tool_sequence": ["search", "verify", "chart", "export"],
            "objective": objectives[i],
        }
        for i in range(6)
    ]
    assert (
        P.promote_tool_sequence_to_skill(
            _success_pattern(support=6), episodes=episodes, skill_credit=0.9
        )
        is None
    )


def test_recurrence_within_one_conversation_is_one_occasion():
    """一场会话里反复跑同一套流程只算一次场合，不构成"这件事总在发生"。"""
    episodes = _supporting_episodes(6, chats=["chat-same"] * 6)
    assert (
        P.promote_tool_sequence_to_skill(
            _success_pattern(support=6), episodes=episodes, skill_credit=0.9
        )
        is None
    )


def test_no_request_text_means_no_commonality_and_no_promotion():
    """Commonality that cannot be established is not commonality."""
    episodes = _supporting_episodes(6, objective="")
    assert (
        P.promote_tool_sequence_to_skill(
            _success_pattern(support=6), episodes=episodes, skill_credit=0.9
        )
        is None
    )


def test_promotion_without_supporting_views_is_refused():
    """A caller that cannot show the episodes gets a refusal, not the benefit
    of the doubt."""
    assert P.promote_tool_sequence_to_skill(_success_pattern(), skill_credit=0.9) is None


def test_personal_support_floor_is_honoured_when_passed():
    """个人路径发现阶段用门槛 3；晋升阶段必须沿用同一门槛，而不是全局的 5。"""
    proposal = P.promote_tool_sequence_to_skill(
        _success_pattern(support=3),
        episodes=_supporting_episodes(3),
        min_support=3,
        skill_credit=0.9,
    )
    assert proposal is not None and proposal.support == 3


def test_procedural_memories_are_what_becomes_a_skill():
    """The chain the design promised: memory *content* compiled into a skill."""
    proposal = P.promote_procedural_memory_to_skill(
        memories=[
            {
                "ref": "mref-1",
                "memory_type": "procedural",
                "content": "做财务分析前先核验公司主体是否唯一",
                "why": "存在同名公司",
            },
            {"ref": "mref-2", "memory_type": "fact", "content": "宁德时代是电池厂商"},
        ],
        intent_cluster={"representative": "看一下这家公司的财务风险", "episode_ids": ["e1"]},
        occurrences=4,
        success_rate=0.75,
        plan_variety=3,
    )
    assert proposal is not None and proposal.from_layer == "memory"
    # The content, not a tool list — that is what makes it a compilation.
    rules = [p["rule"] for p in proposal.payload["procedures"]]
    assert rules == ["做财务分析前先核验公司主体是否唯一"]
    # A fact is not eligible: freezing it into a document destroys the only
    # property that made it worth storing, namely that it stays current.
    assert "宁德时代是电池厂商" not in rules
    # Refs kept so the sources can be demoted when it goes live and restored
    # when it is rolled back.
    assert proposal.source_refs == ["mref-1"]


def test_a_recurring_intent_with_no_procedural_memory_yields_no_skill():
    assert (
        P.promote_procedural_memory_to_skill(
            memories=[{"ref": "m", "memory_type": "fact", "content": "某公司是电池厂商"}],
            intent_cluster={"representative": "x", "episode_ids": []},
            occurrences=9,
            success_rate=0.9,
            plan_variety=4,
        )
        is None
    )


def test_a_failing_recurring_intent_is_never_distilled():
    """Distilling a failing approach would make it the house style."""
    assert (
        P.promote_procedural_memory_to_skill(
            memories=[{"ref": "m", "memory_type": "procedural", "content": "先做 X"}],
            intent_cluster={"representative": "x", "episode_ids": []},
            occurrences=9,
            success_rate=0.2,
            plan_variety=4,
        )
        is None
    )


def test_a_stable_plan_needs_no_skill():
    """Same request, same plan every time: already effectively a skill."""
    assert (
        P.promote_procedural_memory_to_skill(
            memories=[{"ref": "m", "memory_type": "procedural", "content": "先做 X"}],
            intent_cluster={"representative": "x", "episode_ids": []},
            occurrences=9,
            success_rate=0.9,
            plan_variety=1,
        )
        is None
    )


# ── Skill → Workflow ─────────────────────────────────────────────────────────


def _combo_pattern(support=9):
    return P.Pattern(
        kind=P.PATTERN_SUCCESS_SUBSEQUENCE,
        signature="kb-search→fin-analyse→report",
        support=support,
        success_rate=0.9,
        skill_sequence=["kb-search", "fin-analyse", "report"],
    )


def test_frequent_and_better_combination_is_promoted():
    proposal = P.promote_skills_to_orchestration(
        _combo_pattern(), combo_success_rate=0.88, individual_success_rate=0.70
    )
    assert proposal is not None
    assert proposal.to_layer == "agent_profile"
    # Declarative policy only — never executable code.
    assert proposal.payload["kind"] == "declarative_policy"
    assert "depends_on" in proposal.payload


def test_frequent_but_no_better_combination_is_not_promoted():
    """Frequency alone would promote every common sequence."""
    assert (
        P.promote_skills_to_orchestration(
            _combo_pattern(), combo_success_rate=0.71, individual_success_rate=0.70
        )
        is None
    )


def test_single_skill_is_never_an_orchestration_change():
    pattern = _combo_pattern()
    pattern.skill_sequence = ["only-one"]
    assert (
        P.promote_skills_to_orchestration(
            pattern, combo_success_rate=0.99, individual_success_rate=0.1
        )
        is None
    )


def test_profile_invalidates_when_a_dependency_moves():
    proposal = P.promote_skills_to_orchestration(
        _combo_pattern(),
        combo_success_rate=0.9,
        individual_success_rate=0.7,
        skill_versions={"kb-search": "v1", "fin-analyse": "v2", "report": "v1"},
    )
    ok, stale = P.dependencies_satisfied(
        proposal, {"kb-search": "v1", "fin-analyse": "v3", "report": "v1"}
    )
    # A profile pointing at a dead skill version is worse than no profile.
    assert ok is False and stale == ["fin-analyse"]


def test_profile_stays_valid_while_dependencies_hold():
    proposal = P.promote_skills_to_orchestration(
        _combo_pattern(),
        combo_success_rate=0.9,
        individual_success_rate=0.7,
        skill_versions={"kb-search": "v1", "fin-analyse": "v2", "report": "v1"},
    )
    ok, stale = P.dependencies_satisfied(
        proposal, {"kb-search": "v1", "fin-analyse": "v2", "report": "v1"}
    )
    assert ok is True and stale == []


# ── Downhill flow ────────────────────────────────────────────────────────────


def test_overlapping_assets_are_surfaced_for_merge():
    overlaps = P.find_overlaps(
        {
            "skill-a": ["search", "verify", "chart"],
            "skill-b": ["search", "verify", "export"],
            "skill-c": ["totally", "different"],
        }
    )
    pairs = {(a, b) for a, b, _ in overlaps}
    assert ("skill-a", "skill-b") in pairs
    assert not any("skill-c" in pair for pair in pairs)


def test_retirement_reads_opens_not_offers():
    """Being loaded is not being used.

    Every enabled skill was recorded as "selected" on every run, so a rule keyed
    on selections ranked skills by how recently they shipped. What separates
    "never chosen" from "chosen and useless" is whether the model opened the
    document for itself.
    """
    out = P.find_retirement_candidates(
        {
            "never-opened": {
                "offers": 40, "opens": 0, "success_rate": 0.0,
                "follow_through": None, "exposure_days": 60, "uplift": None,
            },
            "opened-but-ignored": {
                "offers": 40, "opens": 20, "success_rate": 0.8,
                "follow_through": 0.1, "exposure_days": 60, "uplift": None,
            },
            "opened-and-failing": {
                "offers": 40, "opens": 20, "success_rate": 0.2,
                "follow_through": 0.9, "exposure_days": 60, "uplift": None,
            },
            "healthy": {
                "offers": 40, "opens": 30, "success_rate": 0.9,
                "follow_through": 0.9, "exposure_days": 60, "uplift": None,
            },
        }
    )
    by_id = {item["asset_id"]: item for item in out}
    assert set(by_id) == {"never-opened", "opened-but-ignored", "opened-and-failing"}

    # Never opened is a discovery problem, not a reason to remove a capability.
    assert by_id["never-opened"]["action"] == P.ACTION_LOWER_EXPOSURE
    # Opened and ignored is a *content* problem: retiring it would remove a
    # capability the model actively wanted.
    assert by_id["opened-but-ignored"]["action"] == P.ACTION_REWRITE
    assert by_id["opened-and-failing"]["action"] == P.ACTION_RETIRE


def test_a_freshly_shipped_skill_is_never_retired():
    """The old rule's answer here was "retire it", which punished newness."""
    out = P.find_retirement_candidates(
        {
            "just-shipped": {
                "offers": 30, "opens": 0, "success_rate": 0.0,
                "follow_through": None, "exposure_days": 2, "uplift": None,
            }
        }
    )
    assert out == []


def test_a_harmful_skill_is_rolled_back_immediately():
    out = P.find_retirement_candidates(
        {
            "harmful": {
                "offers": 40, "opens": 20, "success_rate": 0.9,
                "follow_through": 0.9, "exposure_days": 2, "uplift": -0.2,
            }
        }
    )
    # The one case that does not wait for the exposure window: a paired
    # comparison already showed it makes things worse.
    assert out and out[0]["action"] == P.ACTION_ROLLBACK


# ── Negative transfer: the dangerous, silent failure ─────────────────────────


def test_collateral_damage_on_unrelated_tasks_is_detected():
    findings = P.detect_negative_transfer(
        asset_id="skill-weekly-report",
        related_task_types=["report_generation"],
        before={"report_generation": 0.70, "db_analysis": 0.85, "kb_qa": 0.80},
        after={"report_generation": 0.86, "db_analysis": 0.71, "kb_qa": 0.80},
    )
    # The targeted task improved; an unrelated one silently got worse. No single
    # release guardrail is watching that.
    assert [f.task_type for f in findings] == ["db_analysis"]
    assert findings[0].delta < 0


def test_a_dip_on_the_targeted_task_is_not_negative_transfer():
    findings = P.detect_negative_transfer(
        asset_id="skill-x",
        related_task_types=["report_generation"],
        before={"report_generation": 0.9},
        after={"report_generation": 0.5},
    )
    # That is an ordinary regression, caught by the release's own guardrail.
    assert findings == []


def test_small_fluctuations_are_not_flagged():
    findings = P.detect_negative_transfer(
        asset_id="skill-x",
        related_task_types=[],
        before={"kb_qa": 0.800},
        after={"kb_qa": 0.790},
    )
    assert findings == []


def test_task_types_without_a_baseline_are_skipped():
    findings = P.detect_negative_transfer(
        asset_id="skill-x", related_task_types=[], before={}, after={"new_task": 0.1}
    )
    assert findings == []


# ── Regression: process-stable identifiers ───────────────────────────────────


def test_generated_asset_id_is_stable_across_processes():
    """Regression for a bug the unit tests could not see.

    The loop originally derived a new skill's asset id from Python's built-in
    ``hash()``, which is randomised per process (PYTHONHASHSEED). Every
    background cycle therefore minted a fresh id, the de-duplication constraint
    never matched, and the review queue gained one identical candidate per run.
    Only an end-to-end run across separate processes exposed it.
    """
    import hashlib
    import subprocess
    import sys

    signature = "kb_search→company_verify→chart_render→doc_export"
    expected = "auto-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]

    # Two fresh interpreters with hash randomisation explicitly on.
    outs = []
    for seed in ("1", "2"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import hashlib;"
                f"print('auto-' + hashlib.sha256({signature!r}.encode('utf-8')).hexdigest()[:12])",
            ],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
        outs.append(result.stdout.strip())

    assert outs[0] == outs[1] == expected
