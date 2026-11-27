"""智能路由（mode_slug=auto）的领域规则：闸门、候选集、三种策略、结论与回退、JSON 容错。"""

from __future__ import annotations

import pytest

from core.chat import mode_route_decision as decision
from core.chat import mode_router
from core.chat.mode_types import SLUG_AUTO, SLUG_STANDARD, SLUG_TURBO
from core.chat.modes import ChatModeService
from core.config.store import intent_router_options as options_module
from core.config.store.intent_router_options import IntentRouterOptions
from core.db.models import ChatMessage, ChatSession, UserShadow
from core.db.models.chat_mode import ChatMode
from core.infra.exceptions import BadRequestError


def _mode(db, slug, *, scope="restricted", owner=None, enabled=True, manual=True, sort=10, desc=""):
    db.add(
        ChatMode(
            id=f"m_{slug}_{owner or 'o'}", slug=slug, name=f"名_{slug}", description=desc,
            owner_user_id=owner, tool_scope=scope, mcp_server_ids=[], skill_ids=[], plugin_ids=[],
            manual_invoke_enabled=manual, default_effort="fast", effort_locked=False,
            enabled=enabled, sort_order=sort, is_builtin=False,
        )
    )
    db.commit()


@pytest.fixture
def modes(db_session):
    db_session.add(UserShadow(user_id="u1", user_center_id="u1", username="u1"))
    db_session.commit()
    _mode(db_session, SLUG_STANDARD, scope="all", sort=0)
    _mode(db_session, SLUG_TURBO, sort=1)
    _mode(db_session, "writing", manual=False, sort=2, desc="公文写作")
    _mode(db_session, "off", enabled=False, sort=3)
    _mode(db_session, "mine", owner="u1", sort=4)
    return db_session


def _opts(**kw):
    base = dict(enabled=True, strategy="smart_keep", candidate_slugs=(), fallback_slug="standard",
                timeout_ms=5000)
    base.update(kw)
    return IntentRouterOptions(**base)


@pytest.fixture
def router_env(monkeypatch):
    state = {"options": _opts(), "allowed": True, "current": None}
    monkeypatch.setattr(options_module, "intent_router_options", lambda: state["options"])
    monkeypatch.setattr(mode_router, "user_allowed", lambda db, uid: state["allowed"])
    monkeypatch.setattr(mode_router, "current_mode_slug", lambda db, chat_id, ctx: state["current"])
    return state


def _ctx(**kw):
    return {"user_id": "u1", "chat_id": "c1", "mode_slug": SLUG_AUTO, **kw}


# ── 候选集 ────────────────────────────────────────────────────────────────


def test_candidates_are_enabled_official_modes_without_private_or_auto(modes):
    got = mode_router.load_candidates(modes, "u1", _opts(), explicit_invocation=False)
    assert [c.slug for c in got] == [SLUG_STANDARD, SLUG_TURBO, "writing"]
    assert got[0].restricted is False and got[1].restricted is True


def test_candidate_slugs_config_intersects(modes):
    got = mode_router.load_candidates(
        modes, "u1", _opts(candidate_slugs=("turbo", "standard", "mine", "nope")),
        explicit_invocation=False,
    )
    assert [c.slug for c in got] == [SLUG_STANDARD, SLUG_TURBO]


def test_explicit_invocation_drops_modes_that_forbid_manual_invoke(modes):
    got = mode_router.load_candidates(modes, "u1", _opts(), explicit_invocation=True)
    assert "writing" not in [c.slug for c in got]
    for key, value in (("skill_id", "s1"), ("plugin_id", "p"), ("connector_id", "c"),
                       ("mention_agent_id", "a"), ("mcp_ids", ["m"]),
                       ("explicit_subagent_command", {"agent_id": "a"})):
        assert mode_router.has_explicit_invocation({key: value})
    assert not mode_router.has_explicit_invocation({"skill_ids": None, "mcp_ids": []})


def test_auto_slug_is_reserved_on_create(modes):
    with pytest.raises(BadRequestError):
        ChatModeService(modes).create({"name": "x", "slug": SLUG_AUTO}, owner_user_id=None)
    with pytest.raises(BadRequestError):
        ChatModeService(modes).create({"name": "x", "slug": SLUG_AUTO}, owner_user_id="u1")


# ── 闸门与策略 ────────────────────────────────────────────────────────────


def test_disabled_switch_falls_back_without_model(modes, router_env):
    router_env["options"] = _opts(enabled=False)
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert not plan.needs_model
    result = decision.decided_result(plan)
    assert result["mode_slug"] == SLUG_STANDARD
    assert result["source"] == "fallback" and result["fallback_cause"] == "disabled"


def test_missing_capability_falls_back(modes, router_env):
    router_env["allowed"] = False
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert decision.decided_result(plan)["fallback_cause"] == "no_permission"


def test_real_capability_check_defaults_off(modes, monkeypatch):
    monkeypatch.setattr(options_module, "intent_router_options", lambda: _opts())
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert plan.decided_cause == "no_permission"


def test_single_candidate_needs_no_model(modes, router_env):
    router_env["options"] = _opts(candidate_slugs=("turbo",), fallback_slug="standard")
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert plan.decided_slug == SLUG_TURBO and plan.decided_source == "single_candidate"
    # 回退模式不在候选里时改用候选里的模式，免得落到被排除的模式上
    assert plan.fallback_slug == SLUG_TURBO


def test_first_turn_strategy_sticks_after_first_route(modes, router_env):
    router_env["options"] = _opts(strategy="first_turn")
    assert mode_router.plan_route(modes, _ctx(), "q").needs_model
    router_env["current"] = SLUG_TURBO
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert plan.decided_slug == SLUG_TURBO and plan.decided_source == "first_turn_sticky"


def test_smart_keep_on_strongest_mode_keeps_without_model(modes, router_env):
    router_env["current"] = SLUG_STANDARD
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert plan.decided_slug == SLUG_STANDARD and plan.decided_source == "kept"


def test_smart_keep_only_offers_same_or_stronger_modes(modes, router_env):
    router_env["current"] = SLUG_TURBO
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert plan.needs_model and plan.provisional_slug == SLUG_TURBO
    assert {c.slug for c in plan.allowed()} == {SLUG_STANDARD, SLUG_TURBO, "writing"}
    text = decision.build_instruction(plan, strategy_text="策略正文")
    assert "策略正文" in text and "`turbo`" in text and "当前模式" in text


def test_smart_keep_turns_a_downgrade_into_kept(modes, router_env):
    router_env["current"] = SLUG_STANDARD
    router_env["options"] = _opts(candidate_slugs=("standard", "turbo", "writing"))
    plan = mode_router.plan_route(modes, _ctx(), "q")
    # standard 已是最高档：直接保持；人为构造一个需要问模型的计划来验证降级拦截
    plan.decided_slug = None
    result = decision.resolve_outcome(plan, decision.ModelOutcome(text='{"mode_slug":"turbo"}'))
    assert result["mode_slug"] == SLUG_STANDARD and result["source"] == "kept"
    assert result["switched"] is False


def test_per_turn_routes_freely_even_downwards(modes, router_env):
    router_env["options"] = _opts(strategy="per_turn")
    router_env["current"] = SLUG_STANDARD
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert plan.needs_model
    result = decision.resolve_outcome(
        plan, decision.ModelOutcome(text='{"mode_slug":"turbo","reason":"查资料","confidence":0.9}')
    )
    assert result["mode_slug"] == SLUG_TURBO and result["source"] == "llm"
    assert result["switched"] is True and result["previous_mode_slug"] == SLUG_STANDARD
    assert result["mode_name"] == "名_turbo" and result["confidence"] == 0.9


def test_first_route_upgrade_is_not_a_switch(modes, router_env):
    plan = mode_router.plan_route(modes, _ctx(), "q")
    assert plan.provisional_slug == SLUG_STANDARD  # 第一轮按回退模式装配
    result = decision.resolve_outcome(plan, decision.ModelOutcome(text='{"mode_slug":"turbo"}'))
    assert result["switched"] is False and result["previous_mode_slug"] is None
    assert {c["slug"] for c in result["candidates"]} == {SLUG_STANDARD, SLUG_TURBO, "writing"}


@pytest.mark.parametrize(
    "outcome, cause",
    [
        (decision.ModelOutcome(error="timeout"), "timeout"),
        (decision.ModelOutcome(error="tool_call"), "tool_call"),
        (decision.ModelOutcome(error="model_error"), "model_error"),
        (decision.ModelOutcome(text="我觉得用标准模式"), "parse_error"),
        (decision.ModelOutcome(text='{"mode_slug": "mine"}'), "invalid_slug"),
        (decision.ModelOutcome(text='{"mode_slug": "auto"}'), "invalid_slug"),
    ],
)
def test_every_failure_falls_back(modes, router_env, outcome, cause):
    plan = mode_router.plan_route(modes, _ctx(), "q")
    result = decision.resolve_outcome(plan, outcome)
    assert result["mode_slug"] == SLUG_STANDARD
    assert result["source"] == "fallback" and result["fallback_cause"] == cause


# ── JSON 容错 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        '{"mode_slug": "turbo", "reason": "r", "confidence": 0.7}',
        '```json\n{"mode_slug": "turbo", "reason": "r", "confidence": "0.7"}\n```',
        '<think>先想想 {"mode_slug":"standard"}</think>结论：{"mode_slug": "turbo", "reason": "r"}',
        '好的，判断如下 {"slug": "`turbo`", "confidence": 7} 以上',
    ],
)
def test_parse_decision_is_lenient(raw):
    got = decision.parse_decision(raw)
    assert got["mode_slug"] == "turbo"
    assert got["confidence"] is None or 0.0 <= got["confidence"] <= 1.0


@pytest.mark.parametrize("raw", ["", "standard", "[1,2]", '{"reason": "x"}', "{broken"])
def test_parse_decision_rejects_non_decisions(raw):
    with pytest.raises(decision.DecisionParseError):
        decision.parse_decision(raw)


# ── 当前模式 / 事件 / 配置 ───────────────────────────────────────────────


def test_current_mode_comes_from_previous_assistant_route(db_session):
    db_session.add(UserShadow(user_id="u1", user_center_id="u1", username="u1"))
    db_session.add(ChatSession(chat_id="c1", user_id="u1", title="t"))
    db_session.flush()
    rows = [
        ("m1", "user", {}), ("m2", "assistant", {"intent_route": {"mode_slug": "turbo"}}),
        ("m3", "user", {}), ("m4", "assistant", {"run_id": "r-now"}),
    ]
    for seq, (mid, role, extra) in enumerate(rows, start=1):
        db_session.add(ChatMessage(message_id=mid, chat_id="c1", chat_seq=seq, role=role,
                                   content="x", extra_data=extra))
    db_session.commit()
    # m4 是本轮刚建的助手行：跳过它，读到上一轮
    assert mode_router.current_mode_slug(db_session, "c1", {"message_id": "m4"}) == "turbo"
    assert mode_router.current_mode_slug(db_session, "c1", {"run_id": "r-now"}) == "turbo"
    # 上一条助手消息不是智能路由出来的 → 没有当前模式
    assert mode_router.current_mode_slug(db_session, "c1", {"message_id": "zz"}) is None
    assert mode_router.current_mode_slug(db_session, None, {}) is None


def test_tool_events_match_the_contract():
    question = "问" * 300
    call = mode_router.tool_call_event("intent_x", question)
    assert call["type"] == "tool_call" and call["tool_name"] == "intent_recognition"
    assert call["tool_display_name"] == "用户意图识别"
    assert call["tool_args"] == call["input"] == {"question": "问" * 200}
    result = {"mode_slug": "standard", "fallback_cause": "timeout"}
    done = mode_router.tool_result_event("intent_x", result)
    assert done == {"type": "tool_result", "tool_name": "intent_recognition", "tool_id": "intent_x",
                    "status": "success", "result": result}


def test_route_round_trips_through_context():
    ctx = {"mode_slug": "auto"}
    mode_router.apply_to_context(ctx, {"mode_slug": "turbo", "source": "llm"}, "intent_1")
    assert ctx["routed_mode_slug"] == "turbo"
    assert mode_router.stored_route(ctx)["tool_id"] == "intent_1"
    assert mode_router.route_metadata(ctx) == {"mode_slug": "turbo", "source": "llm"}
    assert mode_router.route_metadata({"mode_slug": "standard"}) is None


def test_options_reader_treats_config_errors_as_disabled(monkeypatch):
    class Broken:
        @staticmethod
        def get_instance():
            raise RuntimeError("db down")

    monkeypatch.setattr(options_module.system_config, "SystemConfigService", Broken)
    assert options_module.intent_router_options() == options_module.DISABLED
    assert options_module.intent_router_enabled() is False


def test_options_reader_parses_values(monkeypatch):
    values = {"intent_router.enable": "true", "intent_router.strategy": "weird",
              "intent_router.candidate_slugs": " standard, turbo ,,standard",
              "intent_router.fallback_slug": "", "intent_router.timeout_ms": "abc"}

    class Fake:
        @staticmethod
        def get_instance():
            return Fake()

        def get(self, key, default=None):
            return values.get(key, default)

    monkeypatch.setattr(options_module.system_config, "SystemConfigService", Fake)
    got = options_module.intent_router_options()
    assert got.enabled and got.strategy == "smart_keep"
    assert got.candidate_slugs == ("standard", "turbo")
    assert got.fallback_slug == "standard" and got.timeout_ms == 5000


def test_persist_route_writes_snapshot_and_payload(db_session, monkeypatch):
    from core.db.models import ChatRun
    from core.runs import ownership

    db_session.add(UserShadow(user_id="u1", user_center_id="u1", username="u1"))
    db_session.add(ChatSession(chat_id="c1", user_id="u1", title="t"))
    db_session.add(ChatRun(run_id="r1", chat_id="c1", user_id="u1", message_id="m1", status="running",
                           request_payload={"mode_slug": "auto"},
                           recovery_snapshot={"kind": "chat", "worker_args": {"context": {"a": 1}}}))
    db_session.commit()

    class Journal:
        def append_operation(self, run_id, *, owner, commit_effect, **kw):
            assert run_id == "r1" and owner == "w1" and kw["operation_type"] == "intent_routed"
            commit_effect(db_session)
            db_session.commit()

    monkeypatch.setattr(ownership, "journal", lambda: Journal())
    mode_router.persist_route("r1", "w1", {"mode_slug": "turbo", "source": "llm"}, "intent_9")
    run = db_session.get(ChatRun, "r1")
    ctx = run.recovery_snapshot["worker_args"]["context"]
    assert ctx["a"] == 1 and ctx["routed_mode_slug"] == "turbo"
    assert ctx["intent_route"]["tool_id"] == "intent_9"
    assert run.request_payload["routed_mode_slug"] == "turbo"


def test_mode_spec_resolution_prefers_routed_slug(monkeypatch):
    from core.agent_runtime import targets

    seen = []

    class Service:
        def __init__(self, db):
            pass

        def resolve(self, slug, user_id):
            seen.append(slug)
            return slug

    import core.chat.modes as modes_module

    monkeypatch.setattr(modes_module, "ChatModeService", Service)
    targets._resolve_mode_spec({"mode_slug": "auto", "routed_mode_slug": "turbo", "user_id": "u"})
    targets._resolve_mode_spec({"mode_slug": "standard", "user_id": "u"})
    assert seen == ["turbo", "standard"]


def test_recognition_card_persists_like_any_tool_record():
    """The worker stores the card through the normal tool log, so history reloads show it."""
    from core.chat.tool_log import SegmentRecorder, build_tool_call_event, build_tool_result_event

    log: list = []
    segments = SegmentRecorder()
    call = mode_router.tool_call_event("intent_1", "帮我写报告")
    emitted = build_tool_call_event(call, "c1", log)
    segments.add_tools(log)
    result = {"mode_slug": "standard", "source": "fallback", "fallback_cause": "timeout"}
    done = build_tool_result_event(mode_router.tool_result_event("intent_1", result), "c1", log)
    segments.add_tools(log)
    assert emitted["tool_display_name"] == "用户意图识别" and done["status"] == "success"
    (record,) = log
    assert record["tool_name"] == "intent_recognition" and record["tool_id"] == "intent_1"
    assert record["tool_args"] == {"question": "帮我写报告"}
    assert record["result"] == result and record["status"] == "success"
    assert segments.payload()
