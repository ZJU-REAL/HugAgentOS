"""评价判定的运行时：从会话历史组请求、模型选择、用量记账、超时回退、结果落库与完成回合时派发。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ToolCallBlock

from core.agent_runtime import evaluation_gating
from core.chat import evaluation_gate as gate
from core.config.store import evaluation_gate_options as gate_options
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession, MessageFeedback, UserShadow
from core.infra.time import utc_now
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class FakeModel:
    def __init__(self, blocks, *, delay=0.0, usage=None, context_size=128_000):
        self.model = "fake-model"
        self.provider_id = "prov-x"
        self.context_size = context_size
        self.blocks, self.delay, self.usage = blocks, delay, usage
        self.calls = []

    async def __call__(self, messages, tools, tool_choice=None):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})

        async def gen():
            await asyncio.sleep(self.delay)
            yield SimpleNamespace(content=[], is_last=False, usage=None)
            yield SimpleNamespace(content=self.blocks, is_last=True, usage=self.usage)

        return gen()


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(UserShadow(user_id="u1", username="u1"))
    session.add(ChatSession(chat_id="c1", user_id="u1", title="t"))
    rows = [("u1m", "user", "帮我写一份三季度分析报告"), ("a1", "assistant", "第一版报告……"),
            ("u2m", "user", "换个说法再写一遍"), ("a2", "assistant", "第二版报告……")]
    for seq, (mid, role, content) in enumerate(rows, start=1):
        session.add(ChatMessage(message_id=mid, chat_id="c1", role=role, chat_seq=seq,
                                content=content, extra_data={}))
    now = utc_now()
    for run_id, mid, payload in (("r1", "a1", {}), ("r2", "a2", {"mode_slug": "standard"})):
        session.add(ChatRun(run_id=run_id, chat_id="c1", user_id="u1", message_id=mid,
                            status="completed", request_payload=payload,
                            created_at=now - timedelta(minutes=1 if run_id == "r2" else 5),
                            completed_at=now, updated_at=now))
    session.add(MessageFeedback(message_id="a1", chat_id="c1", user_id="u1", rating="like",
                                verdict="adopt"))
    session.commit()

    async def in_session(fn, *args, **kwargs):
        return fn(session, *args, **kwargs)

    monkeypatch.setattr(evaluation_gating, "run_in_session", in_session)
    monkeypatch.setattr(gate_options, "evaluation_gate_options",
                        lambda: gate_options.EvaluationGateOptions(enabled=True, timeout_ms=2000))
    yield session
    session.close()


@pytest.fixture
def usage_log(monkeypatch):
    from core.harness import usage

    recorded = []

    async def record(recorder, attempt, **kw):
        recorded.append(attempt)
        return attempt

    monkeypatch.setattr(usage, "record_usage_safely", record)
    return recorded


@pytest.fixture
def models(monkeypatch):
    """Route model selection to a FakeModel; record which provider/mode was asked for."""
    from core.llm.runtime import model_cache
    from core.models import config as config_module

    state = SimpleNamespace(model=None, picked=[], assigned={})
    monkeypatch.setattr(model_cache, "get_provider_model",
                        lambda pid, mode: state.picked.append((pid, mode)) or state.model)
    monkeypatch.setattr(model_cache, "get_main_model",
                        lambda mode: state.picked.append(("main", mode)) or state.model)

    class Service:
        @staticmethod
        def get_instance():
            return Service()

        def resolve(self, role):
            assert role == "evaluation_gate"
            return state.assigned.get(role)

    monkeypatch.setattr(config_module, "ModelConfigService", Service)
    return state


def _stored(db):
    db.expire_all()
    return (db.get(ChatMessage, "a2").extra_data or {}).get(gate.STORAGE_KEY)


@pytest.mark.asyncio
async def test_gate_reads_history_and_stores_the_model_decision(db, models, usage_log):
    models.model = FakeModel([TextBlock(text='{"need_evaluation": false, "reason": "重复追问"}')],
                             usage=SimpleNamespace(input_tokens=800, output_tokens=12))
    decided = await evaluation_gating.run_gate(run_id="r2", chat_id="c1", message_id="a2",
                                               provider_id="prov-main")
    assert decided["need_evaluation"] is False and decided["source"] == "model"
    assert _stored(db) == decided

    (call,) = models.model.calls
    assert call["tools"] == [] and call["tool_choice"] is None
    system, user = call["messages"]
    assert system.role == "system" and "回答评价判定" in system.get_text_content()
    assert '"need_evaluation"' in system.get_text_content()
    prompt = user.get_text_content()
    assert "（用户已评价：满意）" in prompt and "最新这条回答（待判定）" in prompt
    assert prompt.index("第一版报告") < prompt.index("第二版报告")
    # Unassigned role: the chat's own main model, thinking off.
    assert models.picked == [("prov-main", "fast")]
    (attempt,) = usage_log
    assert attempt.metadata == {"source": "evaluation_gate", "role": "evaluation_gate"}
    assert attempt.run_id == "r2" and attempt.status == "success"
    assert attempt.usage.prompt_tokens == 800


@pytest.mark.asyncio
async def test_assigned_role_model_is_used(db, models, usage_log):
    models.model = FakeModel([TextBlock(text='{"need_evaluation": true}')])
    models.assigned["evaluation_gate"] = SimpleNamespace(provider_id="prov-small")
    await evaluation_gating.run_gate(run_id="r2", chat_id="c1", message_id="a2",
                                     provider_id="prov-main")
    assert models.picked == [("prov-small", "fast")]


@pytest.mark.asyncio
async def test_timeout_falls_back_to_needing_evaluation(db, models, usage_log, monkeypatch):
    monkeypatch.setattr(gate_options, "evaluation_gate_options",
                        lambda: gate_options.EvaluationGateOptions(enabled=True, timeout_ms=500))
    models.model = FakeModel([TextBlock(text='{"need_evaluation": false}')], delay=1.0)
    decided = await evaluation_gating.run_gate(run_id="r2", chat_id="c1", message_id="a2")
    assert (decided["need_evaluation"], decided["source"], decided["cause"]) == (True, "fallback",
                                                                                 "timeout")
    assert _stored(db)["cause"] == "timeout"
    assert usage_log[0].status == "timeout"


@pytest.mark.asyncio
async def test_tool_call_and_model_errors_fall_back(db, models, usage_log, monkeypatch):
    models.model = FakeModel([ToolCallBlock(id="t1", name="internet_search", input="{}")])
    decided = await evaluation_gating.run_gate(run_id="r2", chat_id="c1", message_id="a2")
    assert decided["cause"] == "tool_call" and decided["need_evaluation"] is True

    def broken(*_args, **_kwargs):
        raise RuntimeError("no provider")

    from core.llm.runtime import model_cache

    monkeypatch.setattr(model_cache, "get_main_model", broken)
    decided = await evaluation_gating.run_gate(run_id="r2", chat_id="c1", message_id="a2")
    assert decided["cause"] == "model_error" and _stored(db)["source"] == "fallback"


@pytest.mark.asyncio
async def test_off_switch_and_unevaluable_runs_are_not_gated(db, models, usage_log, monkeypatch):
    models.model = FakeModel([TextBlock(text='{"need_evaluation": false}')])
    db.get(ChatRun, "r2").request_payload = {"kind": "chat", "automation_task_id": "t1"}
    db.commit()
    assert await evaluation_gating.run_gate(run_id="r2", chat_id="c1", message_id="a2") is None

    db.get(ChatRun, "r2").request_payload = {}
    db.commit()
    monkeypatch.setattr(gate_options, "evaluation_gate_options", lambda: gate_options.DISABLED)
    assert await evaluation_gating.run_gate(run_id="r2", chat_id="c1", message_id="a2") is None
    assert models.model.calls == [] and _stored(db) is None


@pytest.mark.asyncio
async def test_a_superseded_run_does_not_write_onto_the_rerun(db, models, usage_log):
    models.model = FakeModel([TextBlock(text='{"need_evaluation": false}')])
    db.add(ChatRun(run_id="r2b", chat_id="c1", user_id="u1", message_id="a2", status="running",
                   request_payload={}, created_at=utc_now()))
    db.commit()
    assert await evaluation_gating.run_gate(run_id="r2", chat_id="c1", message_id="a2") is None
    assert _stored(db) is None


def test_fit_window_keeps_the_question_and_answer():
    blocks = [f"[第 {i} 轮] " + "字" * 800 for i in range(30)] + ["问题", "回答"]
    out = evaluation_gating._fit_window("INSTR", blocks, SimpleNamespace(context_size=4_000))
    assert out[-2:] == ["问题", "回答"] and len(out) < len(blocks)
    assert out[0] != blocks[0]  # oldest dropped first
    assert evaluation_gating._fit_window("INSTR", blocks, SimpleNamespace()) == blocks


def test_principles_fall_back_to_the_default_file_on_render_failure(monkeypatch):
    from core.llm import prompt_rendering

    def broken(*_a, **_kw):
        raise RuntimeError("prompt store down")

    # render_kind_segment itself falls back to the shipped file when the stored version fails.
    monkeypatch.setattr(prompt_rendering, "render_active_parts", broken)
    text = evaluation_gating._principles(None)
    assert text.startswith("# 回答评价判定") and "## 不需要评价" in text


def test_missing_principles_are_logged(monkeypatch, caplog):
    from core.llm import prompt_rendering

    monkeypatch.setattr(prompt_rendering, "render_kind_segment", lambda *_a, **_kw: "  ")
    with caplog.at_level("WARNING"):
        assert evaluation_gating._principles(None) == ""
    assert any("rendered empty" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk, expected, spawns", [
    ({"type": "meta"}, True, True),
    ({"type": "meta"}, False, False),          # switch off / not evaluable
    ({"type": "meta", "manual_compaction": True}, True, False),
])
async def test_complete_turn_marks_and_spawns_from_one_decision(monkeypatch, chunk, expected,
                                                                spawns):
    from core.chat import evaluation_gate as gate_module
    from core.chat.runtime import completion

    spawned, written = [], {}
    monkeypatch.setattr(evaluation_gating, "spawn", lambda **kw: spawned.append(kw))
    monkeypatch.setattr(gate_module, "gate_expected", lambda db, run_id: expected)
    monkeypatch.setattr(completion.followups, "_spawn_followup_task", lambda **kw: None)
    monkeypatch.setattr(completion.compaction, "_spawn_compaction_task", lambda **kw: None)
    monkeypatch.setattr(completion.compaction, "_should_schedule_compaction", lambda **kw: False)
    monkeypatch.setattr(completion._workspace_mod, "get_pinned", lambda: [])
    monkeypatch.setattr(completion._workspace_mod, "get_pinned_file_ids", lambda: [])
    monkeypatch.setattr(completion.snapshots, "_flush_thinking", lambda turn: None)
    monkeypatch.setattr(completion.snapshots, "_thinking_payload", lambda turn: None)
    monkeypatch.setattr(completion.snapshots, "_model_steps_payload", lambda turn: None)
    monkeypatch.setattr(completion, "_persist_artifacts", lambda *a, **kw: None)
    monkeypatch.setattr(completion.handoff, "_commit_queued_handoff_in_session",
                        lambda *a, **kw: None)

    class Service:
        def __init__(self, db):
            pass

        def upsert_message(self, **kw):
            written.update(kw["extra_data"])

    monkeypatch.setattr(completion.services_ChatService_module, "ChatService", Service)

    def complete(*_a, commit_effect, **_kw):
        commit_effect(object())  # the completion write runs the decision
        return True

    async def emit(*_a, **_kw):
        return None

    turn = SimpleNamespace(
        compaction_budget_inputs={}, model_name="m", run_started_monotonic=0.0,
        chat_id="c1", current_message_id="a2", context={"model_provider_id": "prov-main"},
        emit=emit, segments=SimpleNamespace(payload=lambda: []), thinking_parts=[],
        thinking_log=[], model_steps=[], round_block=None, tool_calls_log=[],
        journal=SimpleNamespace(save_snapshot=lambda *a, **kw: None, complete=complete),
        run_id="r2", owner="w", full_response="答", offsets=SimpleNamespace(last=1),
        lease_task=SimpleNamespace(cancel=lambda: None), latest_user_message="问",
        user_id="u1", current_chat_seq=4, metadata={},
    )
    await completion.complete_turn(turn, chunk)
    marked = written.get(gate_module.PENDING_KEY) is True
    assert marked is spawns and bool(spawned) is spawns
    if spawns:
        assert spawned == [{"run_id": "r2", "chat_id": "c1", "message_id": "a2",
                            "provider_id": "prov-main"}]
