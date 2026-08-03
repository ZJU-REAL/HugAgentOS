"""分层证据融合技能编译器 V2 — 端到端与边界测试.

验收标准逐条落在测试上：pack 内容寻址且可重放；L3 只影响边界、不生成步骤；
共享技能不含 L1 原文；新技能拿不到证据外的工具授权；L3 冲突不能自动激活；
激活技能可沿血缘反查 L2/L3/Episode；回放集覆盖不足不得晋级。
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import AdminSkill
from core.db.models.evolution import (
    EvolutionAgentProfile,
    EvolutionCandidate,
    EvolutionCreditDecision,
    EvolutionEpisode,
    EvolutionEvidencePack,
    EvolutionMemoryOp,
    EvolutionPromotionLink,
    EvolutionRelease,
    EvolutionTraceEvent,
)
from core.evolution import evidence_contract as EC
from core.evolution import graph_scan as GS
from core.evolution import lineage as LN
from core.evolution.applicability import (
    assess_replay_coverage,
    load_manifest,
    manifest_permits,
)
from core.evolution.evidence_resolver import resolve_capability_pack
from core.evolution.skill_candidate_engine import (
    STATUS_CREATED,
    STATUS_REJECTED,
    SkillCandidateSpec,
    create_skill_candidate,
)

_TABLES = [
    EvolutionEpisode.__table__,
    EvolutionTraceEvent.__table__,
    EvolutionCandidate.__table__,
    EvolutionRelease.__table__,
    EvolutionAgentProfile.__table__,
    EvolutionMemoryOp.__table__,
    EvolutionEvidencePack.__table__,
    EvolutionCreditDecision.__table__,
    EvolutionPromotionLink.__table__,
    AdminSkill.__table__,
]


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine)
    import core.db.engine as engine_mod
    import core.evolution.activation as A
    import core.evolution.loop as L
    import core.memory.weights as weights_mod

    monkeypatch.setattr(engine_mod, "SessionLocal", factory)
    monkeypatch.setattr(A, "SessionLocal", factory)
    monkeypatch.setattr(L, "SessionLocal", factory)
    monkeypatch.setattr(weights_mod, "_cache", {})
    session = factory()
    yield session
    session.close()


def _view(episode_id, *, verdict="success", tools=("tool_a", "tool_b"), user="u1",
          chat=None, tenant="default", task_type="finance", relations=()):
    return {
        "episode_id": episode_id,
        "chat_id": chat or f"c-{episode_id}",
        "user_id": user,
        "tenant_id": tenant,
        "verdict": verdict,
        "task_type": task_type,
        "tool_sequence": list(tools),
        "objective": "查一下项目A的进展",
        "graph_relations": list(relations),
        "graph_refs": [r.get("content_hash", "") for r in relations],
    }


_REL = {
    "relation_id": "grel_dep1",
    "content_hash": "hash-dep1",
    "predicate": "depends_on",
    "confidence": 0.9,
    "workspace_id": "default",
}


# ── Pack：内容寻址、分层边界 ──────────────────────────────────────────────────


def test_same_evidence_resolves_to_the_same_pack_id():
    views = [_view(f"e{i}") for i in range(3)]
    pack_a = resolve_capability_pack(views=views, scope={"level": "workspace"})
    pack_b = resolve_capability_pack(views=views, scope={"level": "workspace"})
    assert pack_a.pack_id == pack_b.pack_id
    assert pack_a.content_hash() == pack_b.content_hash()

    changed = resolve_capability_pack(
        views=views + [_view("e9")], scope={"level": "workspace"}
    )
    assert changed.pack_id != pack_a.pack_id


def test_cross_tenant_episodes_are_dropped_by_the_resolver():
    views = [_view("mine"), _view("theirs", tenant="other-tenant")]
    pack = resolve_capability_pack(views=views, scope={"level": "workspace"})
    assert [e["episode_id"] for e in pack.episodes] == ["mine"]


def test_graph_evidence_carries_relation_id_and_a_bounded_role():
    pack = resolve_capability_pack(
        views=[_view("e1", relations=[_REL])], scope={"level": "workspace"}
    )
    relation = pack.graph_context[0]
    assert relation["relation_id"] == "grel_dep1"
    assert relation["role"] == EC.ROLE_DEPENDENCY
    assert pack.validate() == []


def test_a_step_role_is_structurally_impossible():
    """图谱事实永远不能变成执行步骤——role 白名单没有 step。"""
    pack = EC.CapabilityEvidencePackV2(
        graph_context=[{"relation_id": "grel_x", "role": "step"}]
    )
    assert any(v.startswith("graph_role_not_allowed") for v in pack.validate())


def test_raw_l1_content_in_a_pack_is_a_violation():
    pack = EC.CapabilityEvidencePackV2(
        profile_policy_refs=[{"policy": "p", "content": "用户喜欢深色模式"}]
    )
    assert "profile_raw_content_in_pack" in pack.validate()


def test_functional_predicate_with_two_targets_is_a_conflict():
    pack = EC.CapabilityEvidencePackV2(
        graph_context=[
            {"relation_id": "r1", "role": "dependency", "source": "项目A",
             "predicate": "depends_on", "target": "系统B"},
            {"relation_id": "r2", "role": "dependency", "source": "项目A",
             "predicate": "depends_on", "target": "系统C"},
        ]
    )
    conflicts = pack.graph_conflicts()
    assert conflicts and conflicts[0]["source"] == "项目A"
    manifest = EC.build_manifest(pack, scope_level="workspace", risk_tier="high")
    assert manifest["graph_conflict"] is True


# ── Manifest：L3 只进边界 ────────────────────────────────────────────────────


def test_manifest_routes_each_graph_role_to_its_boundary_only():
    pack = EC.CapabilityEvidencePackV2(
        graph_context=[
            {"relation_id": "r1", "role": "dependency", "source": "项目A",
             "predicate": "depends_on", "target": "系统B"},
            {"relation_id": "r2", "role": "exclusion", "source": "规则R",
             "predicate": "related_to", "target": "场景S"},
            {"relation_id": "r3", "role": "applicability", "source": "项目A",
             "predicate": "related_to", "target": "周报"},
        ],
        required_tools=["tool_a"],
    )
    manifest = EC.build_manifest(pack, scope_level="workspace", risk_tier="high")
    assert [d["relation_id"] for d in manifest["dependencies"]] == ["r1"]
    assert [e["relation_id"] for e in manifest["excludes_when"]] == ["r2"]
    assert [a["relation_id"] for a in manifest["applies_when"]] == ["r3"]
    assert set(manifest["required_graph_relations"]) == {"r1", "r2", "r3"}
    # manifest 里没有任何「步骤」字段可以让 L3 溜进去。
    assert "steps" not in manifest


# ── 强制校验 ─────────────────────────────────────────────────────────────────


def _pack_and_manifest(**overrides):
    pack = resolve_capability_pack(
        views=[_view(f"e{i}", relations=[_REL]) for i in range(3)],
        scope={"level": "workspace"},
        **overrides,
    )
    manifest = EC.build_manifest(pack, scope_level="workspace", risk_tier="high")
    return pack, manifest


def test_a_tool_outside_the_evidence_is_refused():
    """新技能不能获得证据中未使用的工具权限。"""
    pack, manifest = _pack_and_manifest()
    violations = EC.validate_candidate_structure(
        pack=pack,
        manifest=manifest,
        document_tools=["tool_a", "delete_everything"],
        body="## 不适用范围\n无\n",
        scope_level="workspace",
    )
    assert any(v.startswith("tool_not_in_evidence") for v in violations)


def test_secret_material_in_the_body_is_refused():
    pack, manifest = _pack_and_manifest()
    violations = EC.validate_candidate_structure(
        pack=pack,
        manifest=manifest,
        document_tools=["tool_a"],
        body="## 步骤\n1. 使用 api_key: sk-abcdefghijklmnop1234 调用\n",
        scope_level="workspace",
    )
    assert "secret_material_in_body" in violations


def test_a_graph_fact_written_into_the_steps_section_is_refused():
    pack, manifest = _pack_and_manifest()
    pack.graph_context[0].update(
        {"source": "项目A", "relationship": "依赖", "target": "系统B"}
    )
    manifest = EC.build_manifest(pack, scope_level="workspace", risk_tier="high")
    violations = EC.validate_candidate_structure(
        pack=pack,
        manifest=manifest,
        document_tools=["tool_a"],
        body="## 步骤\n1. 执行 项目A 依赖 系统B\n",
        scope_level="workspace",
    )
    assert any(v.startswith("graph_relation_written_as_step") for v in violations)


def test_l2_l3_contradiction_refuses_generation():
    pack, _ = _pack_and_manifest()
    pack.graph_context.append(
        {"relation_id": "r-ex", "role": "exclusion", "source": "规则R",
         "predicate": "related_to", "target": "场景S"}
    )
    pack.procedures.append(
        {"ref": {"external_id": "mref-1"}, "rule": "按旧口径处理", "applies_to": "场景S"}
    )
    manifest = EC.build_manifest(pack, scope_level="workspace", risk_tier="high")
    violations = EC.validate_candidate_structure(
        pack=pack,
        manifest=manifest,
        document_tools=[],
        body="",
        scope_level="workspace",
    )
    assert any(v.startswith("l2_l3_contradiction") for v in violations)


# ── 统一引擎（入库） ─────────────────────────────────────────────────────────


_FAKE_BODY = (
    "## 不适用范围\n"
    "不适用于与项目A无关的查询，也不适用于需要修改数据的任务。\n\n"
    "## 适用条件\n"
    "当用户询问项目A的当前进展、里程碑或阻塞项时使用本技能。\n\n"
    "## 步骤\n"
    "1. 使用 `tool_a` 查询项目A的最新状态，确认返回里包含项目名称与更新时间。\n"
    "2. 核对返回的更新时间在最近一周内，超期则提示用户数据可能滞后。\n\n"
    "## 常见失败与处理\n"
    "查询为空时先确认项目名称拼写，再重试一次；仍为空则如实告知用户没有记录。\n"
)


async def _fake_generate(**kwargs):
    from core.evolution.skill_gen import DraftResult

    return (
        {
            "skill_id": kwargs["skill_id"],
            "display_name": "项目进展核查",
            "description": "查询项目A进展时使用",
            "content": f"---\nname: x\n---\n\n{_FAKE_BODY}",
            "allowed_tools": ["tool_a"],
        },
        DraftResult(ok=True, attempts=1),
    )


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _spec(**overrides):
    defaults = dict(
        asset_id="auto-intent-abc",
        proposer="intent_engine",
        hypothesis="同一意图反复出现",
        views=[_view(f"e{i}", relations=[_REL]) for i in range(3)],
        procedures=[
            {"ref": "mref-1", "user_id": "u1", "rule": "先核验主体", "why": "口径要求"}
        ],
        write_document=True,
    )
    defaults.update(overrides)
    return SkillCandidateSpec(**defaults)


def test_engine_persists_candidate_pack_credit_and_manifest(db, monkeypatch):
    import core.evolution.skill_gen as skill_gen

    monkeypatch.setattr(skill_gen, "generate_skill_document", _fake_generate)

    outcome = create_skill_candidate(
        _spec(), credit={"selected": "skill", "confidence": 0.8,
                         "may_produce_candidate": True}, run_async=_run
    )
    assert outcome["status"] == STATUS_CREATED, outcome

    candidate = db.query(EvolutionCandidate).one()
    assert candidate.evidence_pack_id == outcome["pack_id"]
    change = candidate.ir["changes"][0]
    assert change["manifest"]["evidence_pack_id"] == outcome["pack_id"]
    assert change["document"]["display_name"] == "项目进展核查"
    # 血缘可反查：pack 行存在且哈希一致；归因决策已持久化并被候选引用。
    pack_row = db.query(EvolutionEvidencePack).one()
    assert pack_row.pack_hash == outcome["pack_hash"]
    decision = db.query(EvolutionCreditDecision).one()
    assert candidate.credit_decision["decision_id"] == decision.decision_id


def test_engine_is_idempotent_for_identical_evidence(db, monkeypatch):
    import core.evolution.skill_gen as skill_gen

    monkeypatch.setattr(skill_gen, "generate_skill_document", _fake_generate)
    credit = {"selected": "skill", "confidence": 0.8, "may_produce_candidate": True}

    first = create_skill_candidate(_spec(), credit=credit, run_async=_run)
    second = create_skill_candidate(_spec(), credit=credit, run_async=_run)
    assert first["candidate_id"] == second["candidate_id"]
    assert db.query(EvolutionCandidate).count() == 1
    assert db.query(EvolutionEvidencePack).count() == 1


def test_engine_refuses_a_document_that_widens_tool_authority(db, monkeypatch):
    async def widened(**kwargs):
        from core.evolution.skill_gen import DraftResult

        return (
            {
                "skill_id": kwargs["skill_id"],
                "display_name": "x",
                "description": "y",
                "content": "## 不适用范围\n无\n",
                "allowed_tools": ["tool_a", "rm_rf"],
            },
            DraftResult(ok=True, attempts=1),
        )

    import core.evolution.skill_gen as skill_gen

    monkeypatch.setattr(skill_gen, "generate_skill_document", widened)
    outcome = create_skill_candidate(
        _spec(), credit={"selected": "skill", "confidence": 0.8,
                         "may_produce_candidate": True}, run_async=_run
    )
    assert outcome["status"] == STATUS_REJECTED
    assert outcome["code"] == "structure_violation"
    assert db.query(EvolutionCandidate).count() == 0


# ── 激活：manifest 落盘、血缘落账、冲突闸门 ───────────────────────────────────


def _materialisable_candidate(db, monkeypatch, *, graph_conflict=False, risk="medium",
                              status="replay_passed"):
    import core.evolution.skill_gen as skill_gen

    monkeypatch.setattr(skill_gen, "generate_skill_document", _fake_generate)
    views = [_view(f"e{i}", relations=[_REL]) for i in range(3)]
    if graph_conflict:
        conflicting = dict(_REL, relation_id="grel_dep2", content_hash="hash-dep2")
        views.append(_view("e-conflict", relations=[conflicting]))

    spec = _spec(views=views, risk_tier=risk)
    outcome = create_skill_candidate(
        spec, credit={"selected": "skill", "confidence": 0.8,
                      "may_produce_candidate": True}, run_async=_run
    )
    assert outcome["status"] == STATUS_CREATED, outcome
    candidate = db.query(EvolutionCandidate).one()
    if graph_conflict:
        # 人为把 manifest 标记为冲突（resolver 只有拿到实体文本才能发现冲突，
        # 这里直接测试激活闸门本身）。deepcopy：原地改嵌套 JSON 不会被 flush。
        import copy

        ir = copy.deepcopy(candidate.ir)
        ir["changes"][0]["manifest"]["graph_conflict"] = True
        candidate.ir = ir
    candidate.status = status
    candidate.risk_tier = risk
    db.commit()
    return candidate.candidate_id


def test_materialisation_writes_the_manifest_and_the_lineage(db, monkeypatch):
    from core.evolution import activation as A

    candidate_id = _materialisable_candidate(db, monkeypatch)
    result = A.materialise_skill_candidate(candidate_id, approver="admin")

    row = db.query(AdminSkill).one()
    manifest = load_manifest(row.extra_files)
    assert manifest is not None
    assert manifest["evidence_pack_id"].startswith("pack_")
    assert [d["relation_id"] for d in manifest["dependencies"]] == ["grel_dep1"]

    links = db.query(EvolutionPromotionLink).all()
    relations = {link.relation for link in links}
    assert LN.REL_CONSTRAINED_BY in relations   # L3 → skill
    assert LN.REL_VALIDATED_BY in relations     # episode → skill
    assert LN.REL_COMPILED_FROM in relations    # L2 → skill
    assert result["lineage_links"] == len(links)
    # 图谱证据可以通过 relation_id 反查。
    graph_links = [l for l in links if l.relation == LN.REL_CONSTRAINED_BY]
    assert graph_links[0].source_id == "grel_dep1"


def test_a_graph_conflicted_candidate_cannot_activate_directly(db, monkeypatch):
    from core.evolution import activation as A

    candidate_id = _materialisable_candidate(
        db, monkeypatch, graph_conflict=True, risk="low"
    )
    with pytest.raises(A.ActivationError) as excinfo:
        A.materialise_skill_candidate(candidate_id, approver="admin")
    assert excinfo.value.code == "graph_conflict_requires_review"


def test_a_graph_conflicted_candidate_may_still_enter_shadow(db, monkeypatch):
    from core.evolution import activation as A

    candidate_id = _materialisable_candidate(
        db, monkeypatch, graph_conflict=True, risk="medium"
    )
    result = A.materialise_skill_candidate(candidate_id, approver="admin")
    assert result["stage"] == "shadow"
    assert result["exposed_to"] == "nobody (shadow)"


# ── Replay 覆盖 ──────────────────────────────────────────────────────────────


def _coverage_pack():
    views = [
        _view("ok1", relations=[_REL]),
        _view("ok2"),
        _view("bad1", verdict="failed"),
    ]
    return resolve_capability_pack(views=views, scope={"level": "workspace"})


def test_replay_set_without_failure_cases_is_not_covered():
    pack = _coverage_pack()
    assessment = assess_replay_coverage(pack, ["ok1", "ok2"])
    assert assessment["covered"] is False
    assert "failure_recovery_episode" in assessment["missing"]
    assert "negative_example_episode" in assessment["missing"]


def test_replay_set_covering_both_graph_classes_passes():
    pack = _coverage_pack()
    assessment = assess_replay_coverage(pack, ["ok1", "ok2", "bad1"])
    assert assessment["covered"] is True, assessment


def test_replay_set_missing_the_no_graph_class_is_not_covered():
    views = [
        _view("ok1", relations=[_REL]),
        _view("ok2"),  # 唯一「不带图谱依赖」的场景
        _view("bad1", verdict="failed", relations=[_REL]),
    ]
    pack = resolve_capability_pack(views=views, scope={"level": "workspace"})
    assessment = assess_replay_coverage(pack, ["ok1", "bad1"])
    assert "no_graph_dependency_episode" in assessment["missing"]


# ── 运行时适用性 ─────────────────────────────────────────────────────────────


def test_manifest_excludes_and_declared_task_types_are_enforced():
    manifest = {
        "applies_when": [{"task_type": "finance"}],
        "excludes_when": [{"task_type": "legal"}],
    }
    assert manifest_permits(manifest, task_type="finance")[0] is True
    assert manifest_permits(manifest, task_type="legal")[0] is False
    assert manifest_permits(manifest, task_type="hr")[0] is False
    # 没有上下文 → 不拦（fail-open）。
    assert manifest_permits(manifest)[0] is True
    # 没有 manifest（人写的技能）→ 永远放行。
    assert manifest_permits(None, task_type="legal")[0] is True


def test_an_inactive_graph_dependency_withholds_the_skill():
    manifest = {"dependencies": [{"relation_id": "grel_dep1"}]}
    ok, reason = manifest_permits(
        manifest, relation_active=lambda rid: False
    )
    assert ok is False and "dependency_inactive" in reason
    # 图谱不可达（None）不拦：误拦是能力损失。
    assert manifest_permits(manifest, relation_active=lambda rid: None)[0] is True


def test_exposure_withholds_an_evolved_skill_outside_its_manifest(db):
    from core.evolution.exposure import filter_skill_ids

    db.add(
        AdminSkill(
            skill_id="evo-fin",
            skill_content="x",
            display_name="x",
            description="x",
            extra_files={
                "evolution_manifest.json": '{"excludes_when": [{"task_type": "legal"}]}'
            },
        )
    )
    db.add(
        EvolutionRelease(
            release_id="rel-1",
            candidate_id="cand-1",
            target_kind="skill",
            target_asset_id="evo-fin",
            stage="active",
            traffic_percent=100,
            scope_filter={},
            guardrails={},
        )
    )
    db.commit()

    assert filter_skill_ids(["evo-fin"], user_id="u1", db=db) == ["evo-fin"]
    assert filter_skill_ids(["evo-fin"], user_id="u1", task_type="legal", db=db) == []
    assert filter_skill_ids(
        ["evo-fin"], user_id="u1", task_type="finance", db=db
    ) == ["evo-fin"]


# ── graph_scan ───────────────────────────────────────────────────────────────


def _relation_row(relation_id, *, source="项目A", predicate="depends_on",
                  target="系统B", weight=1.0, seen=3, last_seen=None):
    return {
        "relation_id": relation_id,
        "source": source,
        "predicate": predicate,
        "relationship": "依赖",
        "target": target,
        "confidence": 0.9,
        "seen_count": seen,
        "weight": weight,
        "last_seen_at": (last_seen or datetime.now(timezone.utc)).isoformat(),
    }


def test_an_observed_relation_is_reinforced_automatically():
    ops = GS.scan_graph_relations(
        [_relation_row("r1")], observed_relation_ids={"r1": 2}
    )
    reinforce = [op for op in ops if op.operation == GS.OP_REINFORCE]
    assert reinforce and reinforce[0].auto is True
    assert reinforce[0].payload["weight"] == pytest.approx(1.0)


def test_a_stale_relation_is_down_weighted_never_deleted():
    old = datetime.now(timezone.utc) - timedelta(days=120)
    ops = GS.scan_graph_relations([_relation_row("r1", last_seen=old)])
    assert [op.operation for op in ops] == [GS.OP_REWEIGHT]
    assert ops[0].auto is True
    assert ops[0].payload["weight"] == pytest.approx(0.5)


def test_a_floored_stale_relation_becomes_a_manual_deactivation_proposal():
    old = datetime.now(timezone.utc) - timedelta(days=120)
    ops = GS.scan_graph_relations([_relation_row("r1", weight=0.2, last_seen=old)])
    assert [op.operation for op in ops] == [GS.OP_DEACTIVATE]
    assert ops[0].auto is False


def test_contradictions_are_flagged_for_humans_not_resolved_by_majority():
    ops = GS.scan_graph_relations(
        [_relation_row("r1", target="系统B"), _relation_row("r2", target="系统C")]
    )
    flags = [op for op in ops if op.operation == GS.OP_FLAG_CONTRADICTION]
    assert flags and flags[0].auto is False
    assert sorted(flags[0].payload["targets"]) == ["系统B", "系统C"]


def test_a_clearly_superseded_relation_is_proposed_not_applied():
    old = datetime.now(timezone.utc) - timedelta(days=200)
    ops = GS.scan_graph_relations(
        [
            _relation_row("r-old", target="旧系统", seen=2, last_seen=old),
            _relation_row("r-new", target="新系统", seen=5),
        ]
    )
    supersedes = [op for op in ops if op.operation == GS.OP_SUPERSEDE]
    assert supersedes and supersedes[0].auto is False
    assert supersedes[0].payload["superseded_by"] == "r-new"


def test_repeatedly_mentioned_missing_entities_are_suggested():
    ops = GS.scan_graph_relations(
        [_relation_row("r1")], entity_mentions={"新中台": 4, "系统B": 9}
    )
    suggestions = [op for op in ops if op.operation == GS.OP_SUGGEST_MISSING]
    # 系统B 已在图谱里，不建议；新中台不在，建议。
    assert [op.payload["entity"] for op in suggestions] == ["新中台"]


def test_apply_never_writes_manual_operations(monkeypatch):
    calls = []

    def fake_update(relation_id, *, scope_user_id, weight=None, active=None):
        calls.append((relation_id, weight, active))
        return True

    import core.memory.graph as G

    monkeypatch.setattr(G, "update_relation_state", fake_update)
    ops = [
        GS.GraphOp(operation=GS.OP_REINFORCE, relation_id="r1", payload={"weight": 0.9}),
        GS.GraphOp(operation=GS.OP_FLAG_CONTRADICTION, relation_id="r2"),
        GS.GraphOp(operation=GS.OP_SUPERSEDE, relation_id="r3"),
    ]
    outcome = GS.apply_graph_ops(ops, scope_user_id="u1")
    assert [c[0] for c in calls] == ["r1"]
    assert len(outcome["manual"]) == 2


# ── 血缘账本 ─────────────────────────────────────────────────────────────────


def test_lineage_edges_are_idempotent(db):
    edge = ("memory", "mref-1", LN.REL_COMPILED_FROM, "skill", "evo-x@1.0.0")
    assert len(LN.record_links([edge])) == 1
    assert len(LN.record_links([edge])) == 1
    assert db.query(EvolutionPromotionLink).count() == 1


def test_unknown_link_relations_are_refused(db):
    edges = [("memory", "mref-1", "made_up_edge", "skill", "evo-x@1.0.0")]
    assert LN.record_links(edges) == []
    assert db.query(EvolutionPromotionLink).count() == 0
