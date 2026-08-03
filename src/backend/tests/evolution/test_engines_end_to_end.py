"""End to end: from episodes to a row in the candidate table.

Two review passes over this system produced green suites while two of the three
advertised engines did nothing, and the reason is visible in what the tests
asserted. They checked that a function returned the right dictionary. They could
not have caught either of the failures that actually happened:

* a finding that was computed and then **had no exit** — the function returned
  what it should, and nothing downstream read it;
* a finding computed from **the wrong signal** — the function returned what its
  inputs implied, and the inputs did not mean what the name said.

So the assertions here start from constructed episodes and end at durable state:
a row in ``evolution_candidates``, an entry in the memory-op ledger, a profile
that governs the next run. Anything that cannot be observed at that boundary did
not happen, whatever the intermediate values looked like.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models.evolution import (
    EvolutionAgentProfile,
    EvolutionCandidate,
    EvolutionEpisode,
    EvolutionMemoryOp,
    EvolutionRelease,
    EvolutionTraceEvent,
)
from core.evolution import agent_profile as AP
from core.evolution import cycle_extras as X
from core.evolution import loop as L
from core.evolution import memory_apply as MA
from core.evolution import similarity as S
from core.evolution import trace_assembler as TA
from core.evolution.memory_ops import OP_MERGE, OP_REWEIGHT, MemoryOp

_TABLES = [
    EvolutionEpisode.__table__,
    EvolutionTraceEvent.__table__,
    EvolutionCandidate.__table__,
    EvolutionRelease.__table__,
    EvolutionAgentProfile.__table__,
    EvolutionMemoryOp.__table__,
]


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine)
    for module in (L, MA, AP, X):
        if hasattr(module, "SessionLocal"):
            monkeypatch.setattr(module, "SessionLocal", factory)
    import core.db.engine as engine_mod
    import core.memory.weights as weights_mod

    monkeypatch.setattr(engine_mod, "SessionLocal", factory)
    monkeypatch.setattr(weights_mod, "_cache", {})
    session = factory()
    yield session
    session.close()


def _episode(db, episode_id, *, user="u1", chat=None, objective="", tools=(), verdict="success"):
    db.add(
        EvolutionEpisode(
            episode_id=episode_id,
            message_id=f"m-{episode_id}",
            chat_id=chat or f"c-{episode_id}",
            user_id=user,
            tenant_id="default",
            task_type="finance",
            privacy_class="tenant",
            objective_preview=objective,
            quality_score=0.9 if verdict == "success" else 0.1,
            outcome={"verdict": verdict},
        )
    )
    for index, tool in enumerate(tools, start=1):
        db.add(
            EvolutionTraceEvent(
                event_id=f"{episode_id}-t{index}",
                episode_id=episode_id,
                message_id=f"m-{episode_id}",
                seq=index,
                event_type="tool.called",
                payload={"tool_name": tool, "status": "success"},
            )
        )
    db.commit()


# ── The intent engine reaches the candidate table ────────────────────────────


def test_a_recurring_intent_becomes_a_row_in_the_candidate_table(db, monkeypatch):
    """The failure this exists to catch: a finding computed with nowhere to go.

    The previous implementation produced exactly this finding and put it in a
    dictionary that no code read. Every intermediate assertion passed.
    """
    for i in range(4):
        _episode(
            db,
            f"ep{i}",
            chat=f"chat{i}",
            objective="帮我看下宁德时代这家公司的财务风险",
            tools=[f"tool{i}", "export"],
        )

    monkeypatch.setattr(S, "_embed_all", lambda texts: None)

    # The generator is the one part that talks to a model; its output is fixed
    # here so the test measures the wiring rather than the model.
    async def fake_generate(**kwargs):
        from core.evolution.skill_gen import DraftResult

        return (
            {
                "skill_id": kwargs["skill_id"],
                "display_name": "财务风险核查",
                "description": "评估单一上市公司的财务风险时使用",
                "content": "---\nname: x\n---\n\n## 不适用范围\n无\n\n## 步骤\n1. 核验主体\n",
                "allowed_tools": ["tool0", "export"],
            },
            DraftResult(ok=True, attempts=1),
        )

    import core.evolution.skill_gen as skill_gen

    monkeypatch.setattr(skill_gen, "generate_skill_document", fake_generate)

    views = L.load_episode_views(limit=50)
    assert len(views) == 4

    result = L._run_other_engines(views, dry_run=False)

    rows = db.query(EvolutionCandidate).filter(
        EvolutionCandidate.proposer == "intent_engine"
    ).all()
    assert len(rows) == 1, result["engine_candidates"]
    # The document, not a tool list, is what reaches the runtime.
    assert rows[0].ir["changes"][0]["document"]["display_name"] == "财务风险核查"


def test_a_skill_compiled_from_several_peoples_memories_demotes_each_store(db):
    """Demotion happens inside one person's store.

    A fleet-wide skill can be compiled from more than one person's procedural
    memories. Carrying a bare list of refs leaves the reverse edge with no owner
    to apply them against, so it would silently do nothing for exactly the
    candidates that most need it.
    """
    from core.memory.weights import invalidate, load_overlay

    for owner, ref in (("u1", "mref-a"), ("u2", "mref-b")):
        MA.demote_promoted_memories(
            candidate_id="cand-fleet",
            user_id=owner,
            memory_refs=[ref],
            skill_id="evo-fin",
        )
    invalidate()
    assert load_overlay("u1")[0] == {"mref-a": MA.PROMOTION_WEIGHT}
    assert load_overlay("u2")[0] == {"mref-b": MA.PROMOTION_WEIGHT}

    # And one rollback restores both, because they share the candidate.
    MA.revert_memory_ops(candidate_id="cand-fleet")
    assert load_overlay("u1")[0] == {} and load_overlay("u2")[0] == {}


def test_a_skill_that_cannot_be_written_produces_no_candidate(db, monkeypatch):
    """A draft that fails validation must yield nothing, not a partial asset."""
    for i in range(4):
        _episode(db, f"ep{i}", chat=f"chat{i}",
                 objective="帮我看下宁德时代这家公司的财务风险", tools=[f"tool{i}"])
    monkeypatch.setattr(S, "_embed_all", lambda texts: None)

    async def fails(**_kwargs):
        from core.evolution.skill_gen import DraftResult

        return None, DraftResult(ok=False, violations=["contains_code_block"], attempts=2)

    import core.evolution.skill_gen as skill_gen

    monkeypatch.setattr(skill_gen, "generate_skill_document", fails)

    L._run_other_engines(L.load_episode_views(limit=50), dry_run=False)
    assert db.query(EvolutionCandidate).count() == 0


# ── The memory engine reaches the store ──────────────────────────────────────


def test_an_approved_memory_op_actually_changes_retrieval(db):
    """Approval used to change nothing at all.

    The governance split named ``reweight`` safe to apply automatically, and no
    code path could apply it. A queue whose approvals do nothing is worse than
    no queue: it reports progress that did not occur.
    """
    from core.memory.weights import load_overlay

    outcome = MA.apply_memory_ops(
        [
            MemoryOp(
                operation=OP_REWEIGHT,
                memory_ref="mref-noisy",
                reason="注入 6 次仅 1 次成功",
                before={"weight": 1.0},
                after={"weight": 0.5},
            )
        ],
        candidate_id="cand-1",
        user_id="u1",
        approver="admin",
    )
    assert len(outcome["applied"]) == 1

    weights, superseded = load_overlay("u1", "default")
    assert weights["mref-noisy"] == 0.5 and not superseded


def test_applying_the_same_op_twice_does_not_stack(db):
    """0.5 applied twice is 0.25 — a different decision than the one approved."""
    from core.memory.weights import invalidate, load_overlay

    op = MemoryOp(
        operation=OP_REWEIGHT, memory_ref="m", before={"weight": 1.0}, after={"weight": 0.5}
    )
    MA.apply_memory_ops([op], candidate_id="c", user_id="u1", approver="a")
    MA.apply_memory_ops([op], candidate_id="c", user_id="u1", approver="a")
    invalidate("u1", "default")
    weights, _ = load_overlay("u1", "default")
    assert weights["m"] == 0.5
    assert db.query(EvolutionMemoryOp).count() == 1


def test_a_merge_supersedes_rather_than_deletes(db):
    from core.memory.weights import load_overlay

    MA.apply_memory_ops(
        [MemoryOp(operation=OP_MERGE, memory_ref="dup", after={"superseded_by": "keep"})],
        candidate_id="c",
        user_id="u1",
        approver="a",
    )
    _weights, superseded = load_overlay("u1", "default")
    assert superseded == {"dup"}
    # Nothing was removed: the row survives with its undo attached.
    row = db.query(EvolutionMemoryOp).one()
    assert row.status == "applied" and row.before_state is not None


def test_reverting_restores_the_previous_retrieval_behaviour(db):
    from core.memory.weights import load_overlay

    MA.apply_memory_ops(
        [MemoryOp(operation=OP_REWEIGHT, memory_ref="m", after={"weight": 0.5})],
        candidate_id="cand-x",
        user_id="u1",
        approver="a",
    )
    MA.revert_memory_ops(candidate_id="cand-x")
    weights, superseded = load_overlay("u1", "default")
    assert weights == {} and superseded == set()


def test_an_operation_with_no_executor_is_refused_not_silently_reported(db):
    """Reporting success for something nothing can do is the worst outcome."""
    outcome = MA.apply_memory_ops(
        [MemoryOp(operation="delete", memory_ref="m")],
        candidate_id="c",
        user_id="u1",
        approver="a",
    )
    assert outcome["applied"] == []
    assert outcome["refused"][0]["reason"] == "no_executor_for_operation"


# ── The promotion chain closes ───────────────────────────────────────────────


def test_promoting_a_memory_into_a_skill_demotes_the_source(db):
    """The reverse edge, which existed only as an uncalled function.

    Without it the same knowledge is paid for twice — retrieval and skill — and
    the retrieval half keeps consuming the budget that made compiling it
    worthwhile.
    """
    from core.memory.weights import load_overlay

    MA.demote_promoted_memories(
        candidate_id="cand-9",
        user_id="u1",
        memory_refs=["mref-a", "mref-b"],
        skill_id="evo-fin",
    )
    weights, _ = load_overlay("u1", "default")
    assert weights == {"mref-a": MA.PROMOTION_WEIGHT, "mref-b": MA.PROMOTION_WEIGHT}


def test_rolling_the_skill_back_restores_the_memories(db):
    from core.memory.weights import load_overlay

    MA.demote_promoted_memories(
        candidate_id="cand-9", user_id="u1", memory_refs=["mref-a"], skill_id="evo-fin"
    )
    MA.revert_memory_ops(candidate_id="cand-9")
    weights, _ = load_overlay("u1", "default")
    assert weights == {}


# ── Orchestration governs the next run ───────────────────────────────────────


def test_a_published_profile_governs_the_next_run_without_a_restart(db):
    """The property the old arrangement could not have.

    Loop thresholds were read into module constants at import, so publishing
    required a restart and replicas disagreed until all of them had.
    """
    profile = AP.builtin_profile()
    profile.profile_id = "auto-finance"
    profile.task_types = ["finance"]
    profile.max_react_turns = 12
    db.add(
        EvolutionAgentProfile(
            profile_id=profile.profile_id,
            version="v1",
            task_types=list(profile.task_types),
            scope={},
            payload=profile.to_dict(),
            is_active=True,
        )
    )
    db.commit()

    assert AP.load_active_profile(task_type="finance").max_react_turns == 12
    # A task type this profile does not claim keeps the built-in assembly.
    assert AP.load_active_profile(task_type="chat").profile_id == "builtin"


def test_one_tenants_profile_never_governs_anothers_run(db):
    profile = AP.builtin_profile()
    profile.profile_id = "acme-finance"
    profile.task_types = ["finance"]
    profile.scope = {"tenant_id": "acme"}
    profile.max_react_turns = 7
    db.add(
        EvolutionAgentProfile(
            profile_id=profile.profile_id,
            version="v1",
            task_types=["finance"],
            scope={"tenant_id": "acme"},
            payload=profile.to_dict(),
            is_active=True,
        )
    )
    db.commit()

    assert AP.load_active_profile(task_type="finance", tenant_id="acme").max_react_turns == 7
    # The bug the tenant parameter was added for, asserted at the boundary that
    # matters rather than on the helper that picks.
    assert AP.load_active_profile(task_type="finance", tenant_id="other").profile_id == "builtin"


def test_a_deactivated_profile_stops_governing_immediately(db):
    profile = AP.builtin_profile()
    profile.profile_id = "auto-finance"
    profile.task_types = ["finance"]
    profile.max_react_turns = 12
    row = EvolutionAgentProfile(
        profile_id=profile.profile_id,
        version="v1",
        task_types=["finance"],
        scope={},
        payload=profile.to_dict(),
        is_active=True,
    )
    db.add(row)
    db.commit()
    assert AP.load_active_profile(task_type="finance").profile_id == "auto-finance"

    row.is_active = False
    db.commit()
    assert AP.load_active_profile(task_type="finance").profile_id == "builtin"


def test_a_stored_profile_that_no_longer_validates_is_not_applied(db):
    """Applying it partially would produce a configuration nobody reviewed."""
    broken = AP.builtin_profile()
    broken.profile_id = "broken"
    broken.budget_multiplier = 99.0
    db.add(
        EvolutionAgentProfile(
            profile_id="broken",
            version="v1",
            task_types=[],
            scope={},
            payload=broken.to_dict(),
            is_active=True,
        )
    )
    db.commit()
    assert AP.load_active_profile(task_type="chat").profile_id == "builtin"


# ── Skill opens are recoverable from history ─────────────────────────────────


def test_a_skill_open_is_recovered_from_the_tool_log():
    """The usage signal, derived from arguments the log has always carried."""
    assert TA.skill_id_from_path("/workspace/skills/evo-fin/SKILL.md") == "evo-fin"
    assert TA.skill_id_from_path("/app/storage/sandbox_skills/word-editing/SKILL.md") == (
        "word-editing"
    )
    assert TA.skill_id_from_path("/workspace/report.md") == ""
