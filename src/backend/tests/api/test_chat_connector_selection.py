import asyncio
from types import SimpleNamespace

from pydantic import ValidationError

from api.routes.v1.chats import _build_user_extra_data
from api.schemas import ChatRequest
from core.llm.agent_factory import _required_mcp_server_keys
from core.llm.middlewares import (
    ExplicitConnectorInvocationError,
    ExplicitConnectorToolChoiceMiddleware,
)
from orchestration.workflow import _build_skill_injection


def test_connector_selection_is_persisted_in_user_message_metadata():
    request = ChatRequest(
        chat_id="connector-chat",
        message="查询今天的天气",
        connector_id="internet_search",
        connector_name="联网搜索",
    )

    extra = _build_user_extra_data(request)

    assert extra["mcp_ids"] == ["internet_search"]
    assert extra["connector_id"] == "internet_search"
    assert extra["connector_name"] == "联网搜索"


def test_connector_selection_builds_a_direct_activation_hint():
    hint = _build_skill_injection(
        {
            "mcp_ids": ["internet_search"],
            "connector_id": "internet_search",
            "connector_name": "联网搜索",
        }
    )

    assert hint is not None
    assert "用户已显式选择连接器「联网搜索」" in hint["content"]
    assert "系统会强制先真实调用该连接器暴露的至少一个工具" in hint["content"]
    assert "本插件包含" not in hint["content"]


def test_connector_display_name_cannot_bypass_the_enforceable_id():
    try:
        ChatRequest(
            chat_id="connector-chat",
            message="查询今天的天气",
            connector_name="联网搜索",
        )
    except ValidationError as exc:
        assert "connector_id is required" in str(exc)
    else:  # pragma: no cover - fail closed if validation regresses
        raise AssertionError("connector_name without connector_id must be rejected")


def test_database_connector_resolves_to_concrete_server_keys():
    assert _required_mcp_server_keys(
        ["database_query"],
        ["internet_search", "db_query", "es_query"],
    ) == ["db_query", "es_query"]


def test_explicit_connector_middleware_forces_only_selected_tools_and_records_execution():
    middleware = ExplicitConnectorToolChoiceMiddleware(
        connector_ids=["internet_search"],
        tool_names=["internet_search", "search_news"],
    )
    captured = {}

    async def reasoning_next(**kwargs):
        captured.update(kwargs)
        if False:  # make this an async generator
            yield None

    async def acting_next(**kwargs):
        yield {"tool_result": "ok"}

    async def run():
        async for _ in middleware.on_reasoning(None, {}, reasoning_next):
            pass
        async for _ in middleware.on_acting(
            None,
            {"tool_call": SimpleNamespace(name="internet_search")},
            acting_next,
        ):
            pass

    asyncio.run(run())

    choice = captured["tool_choice"]
    assert choice.mode == "required"
    assert choice.tools == ["internet_search", "search_news"]
    assert middleware._satisfied is True


def test_explicit_connector_middleware_fails_closed_without_a_real_call():
    middleware = ExplicitConnectorToolChoiceMiddleware(
        connector_ids=["internet_search"],
        tool_names=["internet_search"],
    )

    async def reply_next(**kwargs):
        yield "model answered without a tool"

    async def run():
        async for _ in middleware.on_reply(None, {}, reply_next):
            pass

    try:
        asyncio.run(run())
    except ExplicitConnectorInvocationError as exc:
        assert "未完成真实工具调用" in str(exc)
    else:  # pragma: no cover - fail closed if the guard regresses
        raise AssertionError("a connector-selected reply must not finish without a tool call")
