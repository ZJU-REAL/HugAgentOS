from pydantic import ValidationError

from api.routes.v1.chats import _build_user_extra_data
from api.schemas import ChatRequest
from core.llm.agent_factory import _required_mcp_server_keys
from orchestration.workflow import _build_skill_injection


def test_connector_selection_is_persisted_in_user_message_metadata():
    request = ChatRequest(
        chat_id="connector-chat",
        message="查询今天的天气",
        connector_id="internet_search",
        connector_name="联网搜索",
    )

    extra = _build_user_extra_data(request)

    assert "mcp_ids" not in extra
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
    assert "请优先" in hint["content"]
    assert "系统会强制" not in hint["content"]
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
