"""The loop against evidence shaped like real traffic.

The existing benchmark feeds the loop a corpus where one identical tool sequence
repeats eight times.  Real traffic does not look like that — measured on this
deployment's history, every episode's tool sequence was unique — and the loop
had two blockers that only that unrealistic shape hid:

1. every backfilled episode's verdict was ``unknown``, and both mining paths
   require ``success``;
2. a candidate could only come from ≥5 episodes sharing one *identical* full
   sequence, so the pairwise ordering rules — the part that generalises — could
   never become an asset on their own.

These tests pin the corpus shape that broke it.
"""
from core.evolution.cycle import candidate_records as candidate_records_module
from core.evolution.cycle import ordering as ordering_module
from core.evolution.cycle import patterns as patterns_module
from core.evolution.cycle import promotion_constants as promotion_state_module
from core.evolution.cycle import promotion_types as promotion_types_module

import pytest

from core.evolution.cycle import promotion as P


def _varied_corpus():
    """Same ordering lesson, never the same tool set twice.

    Constraint under test: ``verify`` before ``query``. It holds in two task
    types across five different tool-set shapes, and no shape repeats often
    enough to become a sequence pattern.
    """
    shapes = [
        ["verify", "query", "chart"],
        ["verify", "query", "risk"],
        ["verify", "query", "export"],
        ["verify", "query", "chart", "export"],
        ["verify", "query", "risk", "export"],
    ]
    return [
        {
            "episode_id": f"ep-{i}",
            "verdict": "success",
            "task_type": "finance" if i % 2 else "industry_report",
            "tool_sequence": shape,
        }
        for i, shape in enumerate(shapes)
    ]


def test_no_sequence_pattern_survives_a_realistic_corpus():
    """The premise: exact-sequence mining finds nothing when shapes vary."""
    patterns = patterns_module.discover_patterns(_varied_corpus())
    assert [p for p in patterns if p.kind == promotion_state_module.PATTERN_SUCCESS_SUBSEQUENCE] == []


def test_ordering_rules_do_survive_it():
    corpus = _varied_corpus()
    rules = ordering_module.extract_ordering_constraints(corpus)
    pair = next((c for c in rules if (c.before, c.after) == ("verify", "query")), None)
    assert pair is not None
    assert pair.contexts >= 2
    assert set(pair.validated_in) == {"finance", "industry_report"}


def test_the_cycle_emits_a_rules_only_candidate():
    """Where the loop previously produced nothing at all."""
    from core.evolution.cycle import loop as L

    corpus = _varied_corpus()
    rules = ordering_module.extract_ordering_constraints(corpus)
    assert rules, "precondition: the corpus yields rules"

    # No sequence candidate exists, so every rule is uncovered.
    orphans = candidate_records_module.uncovered_constraints(rules, [])
    assert orphans

    decision = L.assign_credit(candidate_records_module.features_for_ordering_rules(orphans))
    assert decision.may_produce_candidate, (
        f"attribution refused a rules-only candidate: "
        f"{decision.selected} conf={decision.confidence}"
    )
    assert decision.selected == "skill"


def test_a_rule_already_carried_by_a_sequence_skill_is_not_duplicated():
    from core.evolution.cycle import loop as L

    rules = ordering_module.extract_ordering_constraints(_varied_corpus())
    covered = [["verify", "query", "chart", "export"]]
    remaining = candidate_records_module.uncovered_constraints(rules, covered)
    assert all(
        not ({c.before, c.after} <= set(covered[0])) for c in remaining
    )


def test_thin_evidence_still_resolves_to_no_update():
    """The floor must stay a floor: one weak observation is not a capability."""
    from core.evolution.cycle import loop as L

    thin = [promotion_types_module.OrderingConstraint(before="a", after="b", support=0, contexts=0)]
    decision = L.assign_credit(candidate_records_module.features_for_ordering_rules(thin))
    assert decision.may_produce_candidate is False


# ── Materialisation of a rules-only candidate ────────────────────────────────


def test_rules_only_skill_renders_parseable_markdown_carrying_the_rules():
    """Both defects in one test: the id field, and rules reaching the body.

    The skill document is the only channel by which a learned rule reaches the
    model, so a rule absent from it is a rule the runtime never receives.
    """
    from core.capabilities.skills.registry import _load_skill_from_str
    from core.evolution.cycle.skill_document import skill_description as _skill_description, skill_label as _skill_label, skill_markdown as _skill_markdown, tools_from_constraints as _tools_from_constraints

    constraints = [
        c.to_dict() for c in ordering_module.extract_ordering_constraints(_varied_corpus())
    ]
    tools = _tools_from_constraints(constraints)
    assert tools and tools.index("verify") < tools.index("query")

    content = _skill_markdown(
        skill_id="evo-abc123def456",
        label=_skill_label(tools, rules_only=True),
        description=_skill_description(tools, constraints, rules_only=True),
        tools=tools,
        evidence=len(_varied_corpus()),
        constraints=constraints,
        rationale="5 条成功执行中顺序稳定成立",
        rules_only=True,
    )

    spec = _load_skill_from_str(content, "evo-abc123def456")
    assert spec.id == "evo-abc123def456"
    assert spec.description
    # An uncontradicted rule ships with its scope stated. Rules that were
    # disproved somewhere are withheld instead (see the test below), so the
    # document never asks the model to route on task type — measurement showed
    # it cannot.
    assert "先 `verify` 再 `query`" in content
    assert "已在 finance、industry_report 类任务中验证" in content


def test_a_contradicted_rule_is_withheld_rather_than_annotated():
    """A rule disproved somewhere is not shipped to the model at all.

    Stating the exception in prose was tried and measured, twice, and both
    framings failed: as a trailing qualifier the model applied the rule in the
    excluded family anyway; stated first and imperatively it applied the
    *reversed* order everywhere. Since a text annotation cannot make a model
    route on task type, handing it a rule that is wrong somewhere costs more
    than the benefit available where it is right.

    The rule survives in the JSON side file for a runtime applier that knows the
    task type — which is where a scope decision belongs.
    """
    from core.evolution.cycle.skill_document import ordering_rules_section as _ordering_rules_section

    section = _ordering_rules_section(
        [
            {
                "before": "verify",
                "after": "query",
                "support": 6,
                "contexts": 2,
                "validated_in": ["finance"],
                "contradicted_in": ["trap"],
            }
        ]
    )
    # Not presented as an applicable rule…
    assert "先 `verify` 再 `query`" not in section
    # …but the withholding is visible rather than silent.
    assert "已扣留" in section
    assert "trap" in section
