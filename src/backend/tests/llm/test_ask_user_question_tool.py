"""Model-facing contract tests for ``ask_user_question``."""

import json

import pytest
from agentscope.tool import Toolkit
from core.llm.tool_collector import ToolCollector
from core.llm.tools import ask_user_question_tool as tool_module


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "top_level_chat": True,
                "turbo_mode": False,
                "disable_tools": False,
                "chat_id": "chat-main",
            },
            True,
        ),
        (
            {
                "top_level_chat": False,
                "turbo_mode": False,
                "disable_tools": False,
                "chat_id": "chat-child",
            },
            False,
        ),
        (
            {
                "top_level_chat": True,
                "turbo_mode": True,
                "disable_tools": False,
                "chat_id": "chat-turbo",
            },
            False,
        ),
        (
            {
                "top_level_chat": True,
                "turbo_mode": False,
                "disable_tools": True,
                "chat_id": "chat-plan",
            },
            False,
        ),
        (
            {
                "top_level_chat": True,
                "turbo_mode": False,
                "disable_tools": False,
                "chat_id": None,
            },
            False,
        ),
    ],
)
def test_registration_policy_only_allows_standard_interactive_root(kwargs, expected):
    assert tool_module.should_register_ask_user_question(**kwargs) is expected


@pytest.mark.asyncio
async def test_registered_schema_matches_dsh_model_contract():
    collector = ToolCollector()
    tool_module.register_ask_user_question(
        collector,
        chat_id="chat-schema",
        interactive=True,
    )
    toolkit = Toolkit(tools=collector.function_tools)

    schema = next(
        item["function"]
        for item in await toolkit.get_tool_schemas()
        if item["function"]["name"] == "ask_user_question"
    )
    question_schema = schema["parameters"]["$defs"]["AskUserQuestionItem"]
    option_schema = schema["parameters"]["$defs"]["AskUserQuestionOption"]
    assert set(question_schema["properties"]) == {
        "id",
        "question",
        "header",
        "options",
        "multi_select",
    }
    assert set(option_schema["properties"]) == {"label", "description"}
    assert question_schema["additionalProperties"] is True
    assert option_schema["additionalProperties"] is True
    assert question_schema["required"] == ["id", "question"]
    assert option_schema["required"] == ["label"]
    assert question_schema["properties"]["header"]["type"] == "string"
    assert question_schema["properties"]["options"]["type"] == "array"
    assert question_schema["properties"]["multi_select"]["type"] == "boolean"
    assert option_schema["properties"]["description"]["type"] == "string"
    assert "maxItems" not in schema["parameters"]["properties"]["questions"]
    assert "recommended" not in option_schema["properties"]
    assert "id" not in option_schema["properties"]
    assert schema["description"] == (
        "Ask the user a concise question when you need confirmation, a choice, "
        "or missing information before proceeding. Send one or more questions, "
        "each with a stable id that will be echoed in the answer."
    )


@pytest.mark.asyncio
async def test_tool_returns_structured_answer_from_suspended_service(monkeypatch):
    captured = {}

    class CaptureToolkit:
        def register_tool_function(self, function, **_kwargs):
            captured["function"] = function

    async def fake_ask(**kwargs):
        captured["ask_kwargs"] = kwargs
        return {
            "status": "answered",
            "answers": [
                {
                    "id": "scope",
                    "selected": ["current"],
                    "selected_labels": ["仅当前页面"],
                    "custom": "整体偏蓝色",
                    "skipped": False,
                },
            ],
        }

    monkeypatch.setattr(tool_module.user_questions, "ask", fake_ask)
    tool_module.register_ask_user_question(
        CaptureToolkit(),
        chat_id="chat-tool",
        interactive=True,
    )

    response = await captured["function"](
        [
            {
                "id": "scope",
                "question": "修改范围？",
                "multi_select": True,
                "options": [
                    {"label": "仅当前页面"},
                    {"label": "全部页面"},
                ],
            },
        ],
    )
    payload = json.loads(response.content[0].text)
    assert payload == {
        "answers": [
            {
                "id": "scope",
                "selected": ["仅当前页面"],
                "custom": "整体偏蓝色",
            },
        ],
    }
    assert captured["ask_kwargs"]["chat_id"] == "chat-tool"
    assert captured["ask_kwargs"]["interactive"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("cancelled", "ASK_CANCELLED"),
        ("timeout", "ASK_TIMEOUT"),
        ("blocked_non_interactive", "NO_PROVIDER"),
    ],
)
async def test_tool_surfaces_non_answer_outcomes_without_retry_loop(
    monkeypatch,
    status,
    code,
):
    captured = {}

    class CaptureToolkit:
        def register_tool_function(self, function, **_kwargs):
            captured["function"] = function

    async def fake_ask(**_kwargs):
        return {"status": status, "answers": []}

    monkeypatch.setattr(tool_module.user_questions, "ask", fake_ask)
    tool_module.register_ask_user_question(
        CaptureToolkit(),
        chat_id="chat-tool-status",
        interactive=True,
    )
    response = await captured["function"](
        [{"id": "continue", "question": "继续吗？"}],
    )
    assert response.state.value == "error"
    assert response.metadata["error"]["code"] == code
    assert response.content[0].text.startswith("Error: ")
