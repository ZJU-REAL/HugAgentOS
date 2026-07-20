from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.tool._response import ToolChunk
from core.config.display_names import TOOL_DISPLAY_NAMES
from core.infra.exceptions import BadRequestError, ServiceUnavailableError
from core.llm.middlewares import OntologyGateMiddleware
from core.ontology.build_validator import OntologyBuildValidator, ensure_ontology_build_valid
from core.ontology.schemas import OntologyPackDocument
from core.ontology.validator import (
    DomainPackValidator,
    activate_runtime_for_asset,
    build_runtime_payload,
    claim_output_review,
    complete_output_review,
    evaluate_output,
    evaluate_tool_call,
    register_runtime_asset_tags,
    render_runtime_prompt,
    requires_output_review,
)
from core.services.ontology_evolution_service import OntologyEvolutionService
from core.services.ontology_service import (
    OntologyService,
    build_user_ontology_runtime,
    resolve_runtime_asset_tags,
)


def _sample_payload() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "configs"
        / "ontology_packs"
        / "enterprise_risk_v1.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _run_middleware_io_inline(monkeypatch) -> None:
    async def _inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "core.llm.middlewares.asyncio",
        SimpleNamespace(to_thread=_inline),
    )


def _runtime() -> dict:
    document = OntologyPackDocument.model_validate(_sample_payload())
    runtime = build_runtime_payload([document], "请分析某公司的企业风险和风险预警")
    runtime["version_ids"] = ["ontov_test"]
    runtime["packs"][0]["version_id"] = "ontov_test"
    return runtime


def test_domain_pack_validates_all_four_layers():
    document, report = DomainPackValidator().validate(
        _sample_payload(),
        known_tools=TOOL_DISPLAY_NAMES,
    )
    assert report.valid is True
    assert document is not None
    assert document.concepts
    assert document.relations
    assert document.constraints
    assert document.workflows


def test_domain_pack_rejects_dangling_relation_and_unknown_tool():
    payload = _sample_payload()
    payload["relations"][0]["object"] = "UnknownConcept"
    payload["constraints"][0]["target"]["tool"] = "missing_tool"
    document, report = DomainPackValidator().validate(
        payload,
        known_tools=TOOL_DISPLAY_NAMES,
    )
    assert document is None
    assert report.valid is False
    assert any("unknown concept" in item.message for item in report.errors)


def test_runtime_is_relevance_cropped_and_prompt_is_bounded():
    runtime = _runtime()
    assert runtime["enabled"] is True
    assert runtime["governance_run_id"].startswith("ontog_")
    assert runtime["output_review"] == {"status": "pending", "owner": None, "count": 0}
    assert runtime["review_level"] == "committee"
    assert len(runtime["packs"][0]["concepts"]) <= 12
    assert "Enterprise" in {item["id"] for item in runtime["packs"][0]["concepts"]}
    assert "RiskEvent" in {item["id"] for item in runtime["packs"][0]["concepts"]}
    assert "enterprise_has_risk_event" in {item["id"] for item in runtime["packs"][0]["relations"]}
    prompt = render_runtime_prompt(runtime)
    assert "<ontology_contract>" in prompt
    assert "enterprise_risk_analysis" in prompt
    assert "enterprise_has_risk_event" in prompt
    assert len(prompt) < 15000


def test_same_workflow_is_not_reactivated_by_subagent_tag():
    runtime = _runtime()
    initial_events = [
        event
        for event in runtime["runtime_events"]
        if event.get("workflow_id") == "enterprise_risk_analysis"
    ]

    events = activate_runtime_for_asset(
        runtime,
        kind="subagent",
        asset_id="ua_risk",
        tags=["ontology:RiskReport"],
    )

    assert events == []
    assert [
        event
        for event in runtime["runtime_events"]
        if event.get("workflow_id") == "enterprise_risk_analysis"
    ] == initial_events


def test_output_review_can_be_claimed_and_completed_only_once():
    runtime = _runtime()
    owner = f"outer_workflow:{runtime['governance_run_id']}"

    assert claim_output_review(runtime, owner=owner) is True
    assert claim_output_review(runtime, owner="child_agent") is False
    complete_output_review(runtime, owner=owner, verdict="pass")
    assert claim_output_review(runtime, owner=owner) is False
    assert runtime["output_review"] == {
        "status": "completed",
        "owner": owner,
        "count": 1,
        "verdict": "pass",
    }


def test_tool_gate_enforces_prerequisite_and_schema_then_passes():
    runtime = _runtime()
    denied = evaluate_tool_call(
        runtime,
        tool_name="get_company_risk_warning",
        tool_input={"company_id": "instance_entity_company-demo"},
        completed_tools=[],
    )
    assert denied.allowed is False
    assert denied.decision == "deny"
    assert "search_company" in denied.violations[0]["reasons"][0]

    passed = evaluate_tool_call(
        runtime,
        tool_name="get_company_risk_warning",
        tool_input={"company_id": "instance_entity_company-demo"},
        completed_tools=["search_company", "get_company_base_info"],
    )
    assert passed.allowed is True
    assert passed.decision == "pass"


def test_output_gate_requires_citations_and_minimum_content():
    runtime = _runtime()
    denied = evaluate_output(runtime, answer="风险较高", citations=[])
    assert denied.allowed is False
    output_violation = next(
        item for item in denied.violations if item["rule_id"] == "risk_report_requires_evidence"
    )
    assert len(output_violation["reasons"]) == 2

    passed = evaluate_output(
        runtime,
        answer="基于已检索到的公开信息，以下内容区分事实、推断和待核验项。" * 4,
        citations=[{"url": "https://example.test/evidence"}],
        completed_tools=[
            "search_company",
            "get_company_base_info",
            "get_company_risk_warning",
        ],
    )
    assert passed.allowed is True


def test_output_gate_skips_required_tools_when_trace_is_unobservable():
    runtime = _runtime()
    decision = evaluate_output(
        runtime,
        answer="基于已检索到的公开信息，以下内容区分事实、推断和待核验项。" * 4,
        citations=[{"url": "https://example.test/evidence"}],
        completed_tools=None,
    )
    assert decision.allowed is True


def test_unmatched_task_does_not_receive_risk_constraints():
    document = OntologyPackDocument.model_validate(_sample_payload())
    runtime = build_runtime_payload([document], "帮我写一首春天的短诗")
    assert runtime["review_level"] == "none"
    assert runtime["packs"][0]["workflows"] == []
    assert runtime["packs"][0]["constraints"] == []
    assert evaluate_output(runtime, answer="春风拂面。", citations=[]).allowed is True


def test_asset_trigger_candidate_only_requires_review_after_activation():
    document = OntologyPackDocument.model_validate(_sample_payload())
    runtime = build_runtime_payload([document], "查一下这家公司")

    assert runtime["review_level"] == "none"
    assert runtime["activation_candidates"]
    assert requires_output_review(runtime) is False

    activate_runtime_for_asset(
        runtime,
        kind="tool",
        asset_id="get_company_base_info",
    )

    assert runtime["review_level"] == "checkpoint"
    assert requires_output_review(runtime) is True


def test_runtime_asset_id_and_tags_monotonically_activate_workflows():
    document = OntologyPackDocument.model_validate(_sample_payload())
    runtime = build_runtime_payload([document], "查一下示例公司最近的情况")
    assert runtime["review_level"] == "none"
    assert runtime["packs"][0]["workflows"] == []

    profile_events = activate_runtime_for_asset(
        runtime,
        kind="tool",
        asset_id="get_company_base_info",
    )
    assert profile_events[0]["workflow_id"] == "enterprise_profile_checkpoint"
    assert runtime["review_level"] == "checkpoint"

    register_runtime_asset_tags(
        runtime,
        kind="skill",
        asset_id="risk-report-skill",
        tags=["ontology:RiskReport"],
    )
    risk_events = activate_runtime_for_asset(
        runtime,
        kind="skill",
        asset_id="risk-report-skill",
    )
    assert risk_events[0]["workflow_id"] == "enterprise_risk_analysis"
    assert risk_events[0]["matched_tags"] == ["ontology:RiskReport"]
    assert runtime["review_level"] == "committee"

    # A later lower-risk asset cannot downgrade the already activated policy.
    activate_runtime_for_asset(
        runtime,
        kind="subagent",
        asset_id="profile-agent",
        tags=["ontology:Enterprise"],
    )
    assert runtime["review_level"] == "committee"


@pytest.mark.asyncio
async def test_middleware_activates_unmatched_tool_workflow_before_dispatch(monkeypatch):
    _run_middleware_io_inline(monkeypatch)
    document = OntologyPackDocument.model_validate(_sample_payload())
    runtime = build_runtime_payload([document], "查一下示例公司最近的情况")
    runtime["version_ids"] = ["ontov_dynamic"]
    runtime["packs"][0]["version_id"] = "ontov_dynamic"
    for candidate in runtime["activation_candidates"]:
        candidate["pack"]["version_id"] = "ontov_dynamic"

    monkeypatch.setattr(
        "core.services.ontology_service.record_enforcement_event",
        lambda payload: None,
    )
    monkeypatch.setattr(
        "core.services.ontology_service.resolve_runtime_asset_tags",
        lambda **kwargs: [],
    )
    middleware = OntologyGateMiddleware(runtime)

    async def _skip_activation_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(middleware, "_audit_activations", _skip_activation_audit)
    agent = SimpleNamespace(
        state=SimpleNamespace(
            user_id="u1",
            chat_id="c1",
            ontology_enabled=True,
            ontology_runtime=runtime,
            context=[],
        )
    )

    async def invoke(name: str, payload: dict) -> ToolResultState:
        async def next_handler(**kwargs):
            yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

        chunks = [
            item
            async for item in middleware.on_acting(
                agent,
                {
                    "tool_call": ToolCallBlock(
                        id=f"tc-{name}",
                        name=name,
                        input=json.dumps(payload, ensure_ascii=False),
                    )
                },
                next_handler,
            )
        ]
        return chunks[-1].state

    assert await invoke("search_company", {"keyword": "示例公司"}) == ToolResultState.SUCCESS
    assert (
        await invoke(
            "get_company_base_info",
            {"company_id": "instance_entity_company-demo"},
        )
        == ToolResultState.SUCCESS
    )
    assert runtime["review_level"] == "checkpoint"
    assert (
        await invoke(
            "get_company_risk_warning",
            {"company_id": "instance_entity_company-demo"},
        )
        == ToolResultState.SUCCESS
    )
    assert runtime["review_level"] == "committee"
    assert any(
        event.get("type") == "ontology_activation"
        and event.get("source") == "tool"
        and event.get("workflow_id") == "enterprise_risk_analysis"
        for event in runtime["runtime_events"]
    )
    assert any(
        event.get("type") == "ontology_gate"
        and event.get("decision") == "pass"
        and event.get("tool_name") == "get_company_risk_warning"
        for event in runtime["runtime_events"]
    )


@pytest.mark.asyncio
async def test_middleware_lazy_loads_tags_before_tool_dispatch(monkeypatch):
    _run_middleware_io_inline(monkeypatch)
    document = OntologyPackDocument.model_validate(_sample_payload())
    runtime = build_runtime_payload([document], "查一下这家公司的近况")
    order = []

    def fake_resolve(**kwargs):
        order.append("lookup")
        register_runtime_asset_tags(
            kwargs["runtime"],
            kind=kwargs["kind"],
            asset_id=kwargs["asset_id"],
            tags=["ontology:Enterprise"],
        )
        return ["ontology:Enterprise"]

    monkeypatch.setattr(
        "core.services.ontology_service.resolve_runtime_asset_tags",
        fake_resolve,
    )
    monkeypatch.setattr(
        "core.services.ontology_service.record_enforcement_event",
        lambda payload: None,
    )
    middleware = OntologyGateMiddleware(runtime)

    async def _skip_activation_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(middleware, "_audit_activations", _skip_activation_audit)
    agent = SimpleNamespace(
        state=SimpleNamespace(
            user_id="u1",
            chat_id="c1",
            ontology_runtime=runtime,
            context=[],
        )
    )

    async def next_handler(**kwargs):
        order.append("dispatch")
        assert runtime["review_level"] == "checkpoint"
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

    chunks = [
        item
        async for item in middleware.on_acting(
            agent,
            {
                "tool_call": ToolCallBlock(
                    id="tc-lazy-tags",
                    name="search_company",
                    input=json.dumps({"keyword": "示例公司"}, ensure_ascii=False),
                )
            },
            next_handler,
        )
    ]

    assert chunks[-1].state == ToolResultState.SUCCESS
    assert order == ["lookup", "dispatch"]
    assert runtime["asset_tags"]["tool"]["search_company"] == ["ontology:Enterprise"]


@pytest.mark.asyncio
async def test_middleware_short_circuits_denied_call(monkeypatch):
    _run_middleware_io_inline(monkeypatch)
    middleware = OntologyGateMiddleware(_runtime())
    audits = []

    monkeypatch.setattr(
        "core.services.ontology_service.resolve_runtime_asset_tags",
        lambda **kwargs: [],
    )

    async def fake_audit(*args, **kwargs):
        audits.append((args, kwargs))

    monkeypatch.setattr(middleware, "_audit", fake_audit)
    called = False

    async def next_handler(**kwargs):
        nonlocal called
        called = True
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

    tool_call = ToolCallBlock(
        id="tc1",
        name="get_company_risk_warning",
        input=json.dumps({"company_id": "instance_entity_company-demo"}, ensure_ascii=False),
    )
    agent = SimpleNamespace(state=SimpleNamespace(user_id="u1", chat_id="c1"))
    chunks = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": tool_call},
            next_handler,
        )
    ]
    assert called is False
    assert chunks[-1].state == ToolResultState.DENIED
    assert "ONTOLOGY_GATE_DENIED" in chunks[-1].content[0].text
    assert audits


@pytest.mark.asyncio
async def test_disabled_middleware_is_exact_pass_through():
    middleware = OntologyGateMiddleware({"enabled": False})
    called = False

    async def next_handler(**kwargs):
        nonlocal called
        called = True
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

    tool_call = ToolCallBlock(id="tc2", name="any_tool", input="{}")
    agent = SimpleNamespace(state=SimpleNamespace(user_id="u1", chat_id="c1"))
    chunks = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": tool_call},
            next_handler,
        )
    ]
    assert called is True
    assert chunks[-1].state == ToolResultState.SUCCESS


@pytest.mark.asyncio
async def test_middleware_persists_strategy_and_circuit_breaker_markers(monkeypatch):
    _run_middleware_io_inline(monkeypatch)
    middleware = OntologyGateMiddleware(_runtime())
    audits = []
    scheduled = []

    monkeypatch.setattr(
        "core.services.ontology_service.resolve_runtime_asset_tags",
        lambda **kwargs: [],
    )

    async def fake_audit(*args, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(middleware, "_audit", fake_audit)
    monkeypatch.setattr(
        "core.services.ontology_evolution_service.schedule_ontology_evolution",
        lambda **kwargs: scheduled.append(kwargs) or True,
    )

    async def next_handler(**kwargs):
        raise AssertionError("denied tool must never execute")
        yield  # pragma: no cover

    tool_call = ToolCallBlock(
        id="tc-breaker",
        name="get_company_risk_warning",
        input=json.dumps({"company_id": "instance_entity_company-demo"}, ensure_ascii=False),
    )
    agent = SimpleNamespace(state=SimpleNamespace(user_id="u1", chat_id="c1"))
    last = None
    for _ in range(5):
        chunks = [
            item
            async for item in middleware.on_acting(
                agent,
                {"tool_call": tool_call},
                next_handler,
            )
        ]
        last = chunks[-1]
    assert last is not None
    assert last.metadata["ontology_gate"]["circuit_breaker"] is True
    assert audits[-1]["denial_count"] == 5
    assert audits[-1]["circuit_breaker"] is True
    assert scheduled == [{"user_id": "u1"}]


def test_lazy_asset_tag_lookup_is_owner_scoped_and_request_cached(db_session, monkeypatch):
    from core.db import engine as db_engine
    from core.db.models import AdminMcpServer, AdminSkill

    db_session.add_all(
        [
            AdminSkill(
                skill_id="owned-risk-skill",
                skill_content="---\nname: owned-risk-skill\ndescription: risk\n---\n",
                display_name="Owned risk skill",
                description="risk",
                tags=["ontology:RiskReport"],
                owner_user_id="u1",
                is_enabled=False,
            ),
            AdminSkill(
                skill_id="foreign-risk-skill",
                skill_content="---\nname: foreign-risk-skill\ndescription: risk\n---\n",
                display_name="Foreign risk skill",
                description="risk",
                tags=["ontology:RiskReport"],
                owner_user_id="u2",
            ),
            AdminMcpServer(
                server_id="public-company-mcp",
                display_name="Public company MCP",
                description="company",
                transport="streamable_http",
                url="https://example.test/mcp",
                is_enabled=True,
                extra_config={"ontology_tags": ["ontology:Enterprise"]},
                tools_json=[{"name": "search_company"}],
            ),
            AdminMcpServer(
                server_id="foreign-company-mcp",
                display_name="Foreign company MCP",
                description="company",
                transport="streamable_http",
                url="https://example.test/private-mcp",
                owner_user_id="u2",
                extra_config={"ontology_tags": ["ontology:RiskReport"]},
                tools_json=[{"name": "search_company"}],
            ),
        ]
    )
    db_session.commit()

    service = OntologyService(db_session)
    assert service.resolve_asset_tags(kind="skill", asset_id="owned-risk-skill", user_id="u1") == [
        "ontology:RiskReport"
    ]
    assert (
        service.resolve_asset_tags(kind="skill", asset_id="foreign-risk-skill", user_id="u1") == []
    )
    assert service.resolve_asset_tags(kind="tool", asset_id="search_company", user_id="u1") == [
        "ontology:Enterprise"
    ]
    monkeypatch.setattr(
        "core.agent_skills.loader.get_skill_loader",
        lambda: SimpleNamespace(
            load_all_metadata=lambda: {
                "builtin-profile-skill": SimpleNamespace(tags=["ontology:Enterprise"])
            }
        ),
    )
    assert service.resolve_asset_tags(
        kind="skill", asset_id="builtin-profile-skill", user_id="u1"
    ) == ["ontology:Enterprise"]

    class BorrowedSession:
        def __enter__(self):
            return db_session

        def __exit__(self, exc_type, exc, traceback):
            return False

    calls = []
    original = OntologyService.resolve_asset_tags

    def spy(self, **kwargs):
        calls.append((kwargs["kind"], kwargs["asset_id"]))
        return original(self, **kwargs)

    monkeypatch.setattr(db_engine, "SessionLocal", BorrowedSession)
    monkeypatch.setattr(OntologyService, "resolve_asset_tags", spy)
    runtime = build_runtime_payload(
        [OntologyPackDocument.model_validate(_sample_payload())],
        "查一下这家公司的近况",
    )

    first = resolve_runtime_asset_tags(
        runtime=runtime,
        kind="skill",
        asset_id="owned-risk-skill",
        user_id="u1",
    )
    second = resolve_runtime_asset_tags(
        runtime=runtime,
        kind="skill",
        asset_id="owned-risk-skill",
        user_id="u1",
    )

    assert first == second == ["ontology:RiskReport"]
    assert calls == [("skill", "owned-risk-skill")]


def test_lazy_asset_tag_lookup_fails_closed(monkeypatch):
    from core.db import engine as db_engine

    def fail_session():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db_engine, "SessionLocal", fail_session)
    runtime = build_runtime_payload(
        [OntologyPackDocument.model_validate(_sample_payload())],
        "查一下这家公司的近况",
    )

    with pytest.raises(ServiceUnavailableError, match="本次资产调用已停止"):
        resolve_runtime_asset_tags(
            runtime=runtime,
            kind="skill",
            asset_id="risk-report-skill",
            user_id="u1",
        )


def test_service_versions_activation_and_build_validation(db_session):
    from core.db.models import AdminMcpServer

    service = OntologyService(db_session)
    version = service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)
    runtime = service.build_runtime(task="生成企业风险画像")
    assert version.status == "active"
    assert runtime["review_level"] == "committee"
    assert runtime["asset_tags"] == {"tool": {}, "skill": {}, "subagent": {}}

    skill_tags = service.list_asset_tag_options("skill")
    assert [item["value"] for item in skill_tags] == [
        "ontology:Enterprise",
        "ontology:RiskReport",
    ]
    assert service.list_asset_tag_options("subagent") == skill_tags
    risk_option = next(item for item in skill_tags if item["value"] == "ontology:RiskReport")
    assert risk_option["workflows"] == [
        {
            "workflow_ref": "enterprise_risk:enterprise_risk_analysis",
            "workflow_name": "企业风险分析",
            "review_level": "committee",
            "risk": "high",
        }
    ]

    db_session.add(
        AdminMcpServer(
            server_id="ai_chain_information_mcp",
            display_name="产业知识中心查询",
            description="企业画像与风险查询",
            transport="streamable_http",
            url="https://example.test/mcp",
            tools_json=[
                {"name": "search_company", "inputSchema": {}},
                {"name": "get_company_base_info", "inputSchema": {}},
                {"name": "get_company_risk_warning", "inputSchema": {}},
            ],
        )
    )
    db_session.commit()

    invalid = OntologyBuildValidator(db_session).validate(
        asset_type="skill",
        name="企业风险分析技能",
        description="生成企业风险和风险预警报告",
    )
    assert invalid.valid is False
    missing_issue = next(item for item in invalid.errors if item.code == "missing_required_tools")
    assert "产业知识中心查询" in missing_issue.message
    assert "get_company" not in missing_issue.message
    assert missing_issue.details["recommended_mcp_servers"] == [
        {
            "server_id": "ai_chain_information_mcp",
            "display_name": "产业知识中心查询",
            "provided_tools": [
                {"name": "get_company_base_info", "display_name": "企业基本信息"},
                {"name": "get_company_risk_warning", "display_name": "企业风险预警"},
                {"name": "search_company", "display_name": "企业搜索"},
            ],
        }
    ]
    assert invalid.suggestions == [
        "绑定 MCP“产业知识中心查询”（提供：企业基本信息、企业风险预警、企业搜索）"
    ]

    valid = OntologyBuildValidator(db_session).validate(
        asset_type="skill",
        name="企业风险分析技能",
        description="生成企业风险和风险预警报告",
        tool_names=["search_company", "get_company_base_info", "get_company_risk_warning"],
    )
    assert valid.valid is True
    assert valid.resolved_tool_details == [
        {"name": "get_company_base_info", "display_name": "企业基本信息"},
        {"name": "get_company_risk_warning", "display_name": "企业风险预警"},
        {"name": "search_company", "display_name": "企业搜索"},
    ]

    invalid_tool = OntologyBuildValidator(db_session).validate(
        asset_type="tool",
        name="企业风险查询工具",
        description="查询企业风险预警",
        tool_names=["get_company_risk_warning"],
        tool_schemas={
            "get_company_risk_warning": {
                "type": "object",
                "properties": {"wrong_name": {"type": "string"}},
            }
        },
    )
    assert invalid_tool.valid is False
    assert any(item.code == "missing_ontology_parameters" for item in invalid_tool.errors)

    valid_tool = OntologyBuildValidator(db_session).validate(
        asset_type="tool",
        name="企业风险查询工具",
        description="查询企业风险预警",
        tool_names=["get_company_risk_warning"],
        tool_schemas={
            "get_company_risk_warning": {
                "type": "object",
                "properties": {"company_id": {"type": "string"}},
            }
        },
    )
    assert valid_tool.valid is True

    invalid_tag = OntologyBuildValidator(db_session).validate(
        asset_type="skill",
        name="普通技能",
        ontology_tags=["utility", "ontology:InventedConcept"],
    )
    assert invalid_tag.valid is False
    assert any(item.code == "unknown_ontology_tags" for item in invalid_tag.errors)
    valid_tag = OntologyBuildValidator(db_session).validate(
        asset_type="skill",
        name="企业主体技能",
        ontology_tags=["utility", "ontology:Enterprise"],
        tool_names=["search_company", "get_company_base_info"],
    )
    assert valid_tag.valid is True


def test_build_validation_resolves_skill_and_plugin_bindings(db_session):
    from core.db.models import AdminSkill, InstalledPlugin

    service = OntologyService(db_session)
    service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)
    db_session.add(
        AdminSkill(
            skill_id="risk-tool-bundle",
            skill_content="---\nname: risk-tool-bundle\ndescription: 风险工具包\n---\n",
            display_name="风险工具包",
            description="提供企业风险分析工具",
            allowed_tools=[
                "search_company",
                "get_company_base_info",
                "get_company_risk_warning",
            ],
        )
    )
    db_session.add(
        InstalledPlugin(
            install_id="risk-plugin@global",
            slug="risk-plugin",
            name="风险插件",
            source="builtin",
            component_ids={"skills": ["risk-tool-bundle"], "mcp": [], "prompts": []},
        )
    )
    db_session.commit()

    via_skill = OntologyBuildValidator(db_session).validate(
        asset_type="subagent",
        name="企业风险分析助手",
        description="生成企业风险预警报告",
        skill_ids=["risk-tool-bundle"],
    )
    assert via_skill.valid is True
    assert via_skill.resolved_tools == [
        "get_company_base_info",
        "get_company_risk_warning",
        "search_company",
    ]

    via_plugin = OntologyBuildValidator(db_session).validate(
        asset_type="subagent",
        name="企业风险分析助手",
        description="生成企业风险预警报告",
        plugin_ids=["risk-plugin@global"],
    )
    assert via_plugin.valid is True
    assert via_plugin.resolved_tools == via_skill.resolved_tools


def test_common_build_gate_blocks_user_agent_service_bypass(db_session):
    from core.db.models import AdminMcpServer
    from core.infra.exceptions import BadRequestError
    from core.services.user_agent_service import UserAgentService

    service = OntologyService(db_session)
    service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)

    with pytest.raises(BadRequestError) as exc_info:
        UserAgentService(db_session).create(
            user_id="builder-user",
            operator_name="builder",
            owner_type="user",
            data={
                "name": "企业风险分析助手",
                "description": "生成企业风险预警报告",
                "system_prompt": "按领域流程分析风险。",
            },
        )
    assert any(item["code"] == "missing_required_tools" for item in exc_info.value.data["errors"])

    db_session.add(
        AdminMcpServer(
            server_id="risk-mcp",
            display_name="企业风险 MCP",
            description="企业信息和风险预警",
            transport="streamable_http",
            url="https://example.test/mcp",
            tools_json=[
                {"name": "search_company", "inputSchema": {}},
                {"name": "get_company_base_info", "inputSchema": {}},
                {"name": "get_company_risk_warning", "inputSchema": {}},
            ],
        )
    )
    db_session.commit()
    created = UserAgentService(db_session).create(
        user_id="builder-user",
        operator_name="builder",
        owner_type="user",
        data={
            "name": "企业风险分析助手",
            "description": "生成企业风险预警报告",
            "system_prompt": "按领域流程分析风险。",
            "mcp_server_ids": ["risk-mcp"],
            "ontology_tags": ["ontology:Enterprise", "ontology:RiskReport"],
        },
    )
    assert created["mcp_server_ids"] == ["risk-mcp"]
    assert created["ontology_tags"] == ["ontology:Enterprise", "ontology:RiskReport"]


def test_skill_mcp_bindings_and_ontology_workflows_are_persisted_in_skill_md(
    db_session, monkeypatch
):
    admin_skills = pytest.importorskip("api.routes.v1.admin_skills")
    _build_skill_content = admin_skills._build_skill_content
    _extract_mcp_server_ids = admin_skills._extract_mcp_server_ids
    _resolve_mcp_bindings = admin_skills._resolve_mcp_bindings
    _resolve_ontology_workflows = admin_skills._resolve_ontology_workflows
    from core.agent_skills.registry import _load_skill_metadata_from_str
    from core.db.models import AdminMcpServer

    service = OntologyService(db_session)
    service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)
    db_session.add(
        AdminMcpServer(
            server_id="risk-mcp",
            display_name="企业风险查询",
            description="企业信息和风险预警",
            transport="streamable_http",
            url="https://example.test/mcp",
            tools_json=[
                {"name": "search_company", "inputSchema": {}},
                {"name": "get_company_base_info", "inputSchema": {}},
                {"name": "get_company_risk_warning", "inputSchema": {}},
            ],
        )
    )
    db_session.commit()

    mcp_ids, tool_names = _resolve_mcp_bindings(db_session, ["risk-mcp"])
    workflow_refs = _resolve_ontology_workflows(
        db_session, ["ontology:Enterprise", "ontology:RiskReport"]
    )
    content = _build_skill_content(
        skill_id="enterprise-risk-report",
        display_name="企业风险报告",
        description="生成企业风险报告",
        version="1.0.0",
        tags=["report", "ontology:Enterprise", "ontology:RiskReport"],
        allowed_tools=tool_names,
        instructions="按企业风险流程生成报告。",
        mcp_server_ids=mcp_ids,
        ontology_workflows=workflow_refs,
    )

    assert "ontology_tags: ontology:Enterprise, ontology:RiskReport" in content
    assert "enterprise_risk:enterprise_risk_analysis" in content
    assert "enterprise_risk:enterprise_profile_checkpoint" in content
    assert "mcp_servers: risk-mcp" in content
    assert "allowed_tools: search_company get_company_base_info get_company_risk_warning" in content
    assert _extract_mcp_server_ids(content) == ["risk-mcp"]
    metadata = _load_skill_metadata_from_str(content, "enterprise-risk-report")
    assert metadata.allowed_tools == tool_names
    assert metadata.mcp_server_ids == ["risk-mcp"]
    from core.llm import agent_factory

    monkeypatch.setattr(
        agent_factory,
        "get_skill_loader",
        lambda: SimpleNamespace(load_all_metadata=lambda: {"enterprise-risk-report": metadata}),
    )
    assert agent_factory._mcp_ids_bound_to_skills(["enterprise-risk-report"]) == ["risk-mcp"]
    report = OntologyBuildValidator(db_session).validate(
        asset_type="skill",
        name="企业风险报告",
        description="生成企业风险报告",
        mcp_server_ids=mcp_ids,
        ontology_tags=["ontology:Enterprise", "ontology:RiskReport"],
    )
    assert report.valid is True


def test_common_build_gate_exposes_structured_error(db_session):
    from core.infra.exceptions import BadRequestError

    service = OntologyService(db_session)
    service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)
    with pytest.raises(BadRequestError) as exc_info:
        ensure_ontology_build_valid(
            db_session,
            asset_type="skill",
            name="企业风险分析技能",
            description="生成企业风险预警报告",
            tool_names=["get_company_risk_warning"],
        )
    assert exc_info.value.message == "技能未通过本体构建校验"
    assert exc_info.value.data["valid"] is False


def test_user_runtime_resolver_preserves_opt_out(db_session):
    from core.db.models import UserShadow

    service = OntologyService(db_session)
    service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)
    user = UserShadow(
        user_id="ontology_user",
        username="ontology user",
        extra_data={"ontology_enabled": False},
    )
    db_session.add(user)
    db_session.commit()

    opted_in, runtime = build_user_ontology_runtime(
        user_id=user.user_id,
        task="分析企业风险",
        db=db_session,
    )
    assert opted_in is False
    assert runtime == {"enabled": False, "packs": [], "review_level": "none"}

    user.extra_data = {"ontology_enabled": True}
    db_session.commit()
    opted_in, runtime = build_user_ontology_runtime(
        user_id=user.user_id,
        task="分析企业风险",
        db=db_session,
    )
    assert opted_in is True
    assert runtime["review_level"] == "committee"


def test_explicit_user_correction_enters_human_review_queue(db_session):
    from core.db.models import UserShadow
    from core.services.chat_service import ChatService

    service = OntologyService(db_session)
    service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)
    user = UserShadow(
        user_id="correction_user",
        username="correction user",
        extra_data={"ontology_enabled": True},
    )
    db_session.add(user)
    db_session.commit()
    chat = ChatService(db_session)
    chat.ensure_session("correction_chat", user.user_id)
    chat.add_message(
        "correction_chat",
        "user",
        "请生成企业风险与风险预警报告",
        message_id="correction_prompt",
    )
    chat.add_message(
        "correction_chat",
        "assistant",
        "待纠正的风险报告",
        message_id="correction_answer",
    )

    evolution = OntologyEvolutionService(db_session)
    created = evolution.ingest_user_correction(
        user_id=user.user_id,
        chat_id="correction_chat",
        message_id="correction_answer",
        feedback_id="42",
        comment="这条结论缺少风险发生时间和来源证据。",
    )
    assert len(created) == 1
    draft = service.repo.get_draft(created[0])
    assert draft.source_type == "user_correction"
    assert draft.review_status == "pending"
    redacted = evolution.ingest_user_correction(
        user_id=user.user_id,
        chat_id="correction_chat",
        message_id="correction_answer",
        feedback_id="43",
        comment="请联系 13800138000 核对这条风险结论。",
    )
    assert "[REDACTED:phone_cn]" in service.repo.get_draft(redacted[0]).evidence[0]
    assert (
        evolution.ingest_user_correction(
            user_id=user.user_id,
            chat_id="correction_chat",
            message_id="correction_answer",
            feedback_id="44",
            comment="内部文件包含的内容禁止进入演进队列。",
        )
        == []
    )
    assert (
        evolution.ingest_user_correction(
            user_id=user.user_id,
            chat_id="correction_chat",
            message_id="correction_answer",
            feedback_id="42",
            comment="重复提交不应重复建草案。",
        )
        == []
    )


@pytest.mark.asyncio
async def test_evolution_prefilters_and_never_auto_activates(db_session, monkeypatch):
    service = OntologyService(db_session)
    active = service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)
    for index in range(3):
        service.repo.create_event(
            {
                "event_id": f"evt_{index}",
                "pack_id": "enterprise_risk",
                "version_id": active.version_id,
                "rule_id": "risk_query_requires_enterprise",
                "stage": "tool",
                "event_type": "tool_call_gate",
                "decision": "deny",
                "mode": "enforce",
                "target": "get_company_risk_warning",
                "details": {"violations": [{"reason": "missing prerequisite"}]},
            }
        )

    evolution = OntologyEvolutionService(db_session)

    async def no_llm(*args, **kwargs):
        return []

    monkeypatch.setattr(evolution, "_llm_proposals", no_llm)
    result = await evolution.generate_candidates(min_occurrences=3)
    assert result["created"] == 1
    draft = service.repo.get_draft(result["items"][0])
    assert draft.review_status == "pending"
    assert draft.candidate_type == "false_positive"
    pack = service.repo.get_pack("enterprise_risk")
    assert pack.active_version_id == active.version_id


def test_approved_candidate_materializes_draft_version_only(db_session):
    service = OntologyService(db_session)
    active = service.create_version(_sample_payload(), actor_id="tester", activate=True)
    service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)
    draft = service.repo.create_draft(
        {
            "draft_id": "draft_materialize",
            "pack_id": "enterprise_risk",
            "source_type": "enforcement",
            "candidate_type": "term",
            "proposal": {
                "operation": "add",
                "section": "concepts",
                "value": {
                    "id": "VerificationBoundary",
                    "name": "核验边界",
                    "aliases": [],
                    "definition": "结论当前能够被证据支持的时间与数据范围。",
                    "parent_id": "DomainEntity",
                    "closed_values": [],
                    "tags": ["证据"],
                    "risk": "high",
                },
            },
            "evidence": ["repeated evidence gap"],
            "source_event_ids": [],
            "value_score": 80,
            "review_status": "approved",
        }
    )
    version = OntologyEvolutionService(db_session).materialize_approved_draft(draft.draft_id)
    assert version.status == "draft"
    assert version.version == "1.1.1"
    assert service.repo.get_pack("enterprise_risk").active_version_id == active.version_id
    assert draft.proposal["materialized_version_id"] == version.version_id
    with pytest.raises(BadRequestError, match="已经物化"):
        OntologyEvolutionService(db_session).materialize_approved_draft(draft.draft_id)
