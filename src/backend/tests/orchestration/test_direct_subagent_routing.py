from types import SimpleNamespace

import pytest
from api.routes.v1.chats import _resolve_chat_agent_targets, _strip_direct_mention_prefix
from api.schemas import ChatRequest
from core.llm.subagent_tool import (
    _shared_ontology_runtime,
    build_explicit_subagent_command_hint,
)
from core.services.subagent_routing_service import parse_explicit_subagent_command
from fastapi import HTTPException
from orchestration import workflow


class _FakeUserAgentService:
    def __init__(self, _db):
        self.items = [
            {
                "agent_id": "ua_risk",
                "name": "企业风险分析",
                "is_enabled": True,
            }
        ]

    def get_by_id(self, agent_id: str, *, user_id: str):
        assert user_id == "user_1"
        for item in self.items:
            if item["agent_id"] == agent_id:
                return item
        raise LookupError(agent_id)

    def list_for_user(self, user_id: str):
        assert user_id == "user_1"
        return list(self.items)


def test_explicit_mention_id_resolves_as_per_turn_target(monkeypatch):
    import core.services.user_agent_service as service_module

    monkeypatch.setattr(service_module, "UserAgentService", _FakeUserAgentService)
    request = ChatRequest(
        chat_id="chat_1",
        message="@旧名称 分析企业风险",
        mention_agent_id="ua_risk",
        mention_name="旧名称",
    )

    resolved, persistent_name, execution_message, explicit_command = _resolve_chat_agent_targets(
        SimpleNamespace(), request, "user_1"
    )

    assert persistent_name is None
    assert resolved.agent_id is None
    assert resolved.mention_agent_id == "ua_risk"
    assert resolved.mention_name == "企业风险分析"
    assert execution_message == "分析企业风险"
    assert explicit_command is None


def test_legacy_mention_name_resolves_only_unique_accessible_agent(monkeypatch):
    import core.services.user_agent_service as service_module

    monkeypatch.setattr(service_module, "UserAgentService", _FakeUserAgentService)
    request = ChatRequest(
        chat_id="chat_1",
        message="@企业风险分析 分析企业风险",
        mention_name="企业风险分析",
    )

    resolved, _, execution_message, explicit_command = _resolve_chat_agent_targets(
        SimpleNamespace(), request, "user_1"
    )

    assert resolved.mention_agent_id == "ua_risk"
    assert execution_message == "分析企业风险"
    assert explicit_command is None


def test_unknown_mention_target_is_rejected(monkeypatch):
    import core.services.user_agent_service as service_module

    monkeypatch.setattr(service_module, "UserAgentService", _FakeUserAgentService)
    request = ChatRequest(
        chat_id="chat_1",
        message="分析企业风险",
        mention_agent_id="ua_missing",
    )

    with pytest.raises(HTTPException) as exc_info:
        _resolve_chat_agent_targets(SimpleNamespace(), request, "user_1")

    assert exc_info.value.status_code == 403


def test_direct_mention_prefix_is_not_sent_to_target_agent():
    assert (
        _strip_direct_mention_prefix(
            "@企业风险分析 分析杭州量知是否存在企业风险",
            "企业风险分析",
        )
        == "分析杭州量知是否存在企业风险"
    )
    assert _strip_direct_mention_prefix("普通问题", "企业风险分析") == "普通问题"


@pytest.mark.parametrize(
    ("message", "expected_task"),
    [
        ("调用企业风险分析子智能体 分析杭州量知的风险", "分析杭州量知的风险"),
        ("请调用「企业风险分析」子智能体：核查示例公司经营异常", "核查示例公司经营异常"),
        ("调用企业风险分析，帮我评估某企业风险", "评估某企业风险"),
    ],
)
def test_explicit_natural_language_command_routes_unique_agent(message, expected_task):
    command = parse_explicit_subagent_command(
        message,
        [{"agent_id": "ua_risk", "name": "企业风险分析", "is_enabled": True}],
    )

    assert command is not None
    assert command.agent_id == "ua_risk"
    assert command.task == expected_task


@pytest.mark.parametrize(
    "message",
    [
        "企业风险分析子智能体有什么能力",
        "调用企业风险分析子智能体是否合适？",
        "讨论一下调用企业风险分析子智能体的方案",
    ],
)
def test_agent_discussion_does_not_trigger_direct_routing(message):
    assert (
        parse_explicit_subagent_command(
            message,
            [{"agent_id": "ua_risk", "name": "企业风险分析", "is_enabled": True}],
        )
        is None
    )


def test_duplicate_agent_name_does_not_trigger_ambiguous_direct_routing():
    assert (
        parse_explicit_subagent_command(
            "调用企业风险分析子智能体 分析杭州量知的风险",
            [
                {"agent_id": "ua_risk_1", "name": "企业风险分析", "is_enabled": True},
                {"agent_id": "ua_risk_2", "name": "企业风险分析", "is_enabled": True},
            ],
        )
        is None
    )


def test_natural_language_command_is_resolved_by_chat_route(monkeypatch):
    import core.services.user_agent_service as service_module

    monkeypatch.setattr(service_module, "UserAgentService", _FakeUserAgentService)
    request = ChatRequest(
        chat_id="chat_1",
        message="调用企业风险分析子智能体 分析杭州量知的风险",
    )

    resolved, persistent_name, execution_message, explicit_command = _resolve_chat_agent_targets(
        SimpleNamespace(), request, "user_1"
    )

    assert persistent_name is None
    assert resolved.mention_agent_id is None
    assert resolved.mention_name is None
    assert execution_message == "分析杭州量知的风险"
    assert explicit_command is not None
    assert explicit_command.agent_id == "ua_risk"


def test_parent_and_child_share_the_same_ontology_runtime_object():
    runtime = {
        "enabled": True,
        "governance_run_id": "ontog_shared",
        "review_level": "checkpoint",
    }
    agent_ref = {"agent": SimpleNamespace(state=SimpleNamespace(ontology_runtime=runtime))}

    child_runtime = _shared_ontology_runtime(agent_ref)

    assert child_runtime is runtime
    child_runtime["review_level"] = "committee"
    assert runtime["review_level"] == "committee"


def test_only_persistent_agent_chat_uses_direct_route():
    assert (
        workflow._direct_agent_id_from_context(
            {
                "agent_id": None,
                "mention_agent_id": "ua_risk",
                # A stale value from the former @ direct-routing behavior must
                # not bypass the main model either.
                "direct_agent_id": "ua_risk",
            }
        )
        is None
    )
    assert (
        workflow._direct_agent_id_from_context(
            {"agent_id": "ua_risk", "direct_agent_id": "ua_risk"}
        )
        == "ua_risk"
    )


@pytest.mark.asyncio
async def test_mention_keeps_normal_main_model_stream(monkeypatch):
    async def _fake_memory_retrieval(*_args, **_kwargs):
        return None

    async def _unexpected_direct(**_kwargs):
        raise AssertionError("@mention must not bypass the main model")
        yield  # pragma: no cover

    monkeypatch.setattr(workflow, "launch_memory_retrieval", _fake_memory_retrieval)
    monkeypatch.setattr(workflow, "_astream_subagent_direct", _unexpected_direct)
    stream = workflow.astream_chat_workflow(
        session_messages=[{"role": "user", "content": "分析企业风险"}],
        user_message="分析企业风险",
        context={
            "user_id": "user_1",
            "chat_id": "chat_1",
            "agent_id": None,
            "mention_agent_id": "ua_risk",
            "direct_agent_id": "ua_risk",
            "ontology_runtime": {},
        },
    )

    first = await anext(stream)
    await stream.aclose()

    assert first["type"] == "thinking"
    assert "分析" in first["message"]


def test_explicit_command_hint_requires_real_call_subagent_tool():
    hint = build_explicit_subagent_command_hint(
        [{"agent_id": "ua_risk", "name": "企业风险分析"}],
        "ua_risk",
    )

    assert "call_subagent" in hint
    assert 'agent_id="ua_risk"' in hint
    assert "不得调用其他工具" in hint


@pytest.mark.asyncio
async def test_natural_language_command_keeps_normal_main_model_stream(monkeypatch):
    async def _fake_memory_retrieval(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow, "launch_memory_retrieval", _fake_memory_retrieval)
    stream = workflow.astream_chat_workflow(
        session_messages=[{"role": "user", "content": "分析杭州量知的风险"}],
        user_message="分析杭州量知的风险",
        context={
            "user_id": "user_1",
            "chat_id": "chat_1",
            "ontology_runtime": {},
            "explicit_subagent_command": {
                "agent_id": "ua_risk",
                "agent_name": "企业风险分析",
                "task": "分析杭州量知的风险",
            },
        },
    )

    first = await anext(stream)
    await stream.aclose()

    assert first["type"] == "thinking"
    assert "分析" in first["message"]


@pytest.mark.asyncio
async def test_subagent_log_scope_is_closed_before_outer_stream_yields():
    from core.services import log_service

    async def source():
        assert log_service.current_subagent_log_id() == "sublog_1"
        yield ("text_delta", "first")
        assert log_service.current_subagent_log_id() == "sublog_1"
        yield ("text_delta", "second")

    iterator = source().__aiter__()
    first = await workflow._anext_in_subagent_log_scope(iterator, "sublog_1")
    assert first == ("text_delta", "first")
    assert log_service.current_subagent_log_id() is None

    second = await workflow._anext_in_subagent_log_scope(iterator, "sublog_1")
    assert second == ("text_delta", "second")
    assert log_service.current_subagent_log_id() is None

    with pytest.raises(StopAsyncIteration):
        await workflow._anext_in_subagent_log_scope(iterator, "sublog_1")
    assert log_service.current_subagent_log_id() is None
