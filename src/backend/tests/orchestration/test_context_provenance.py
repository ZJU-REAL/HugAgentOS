"""System-provided orchestration hints must not masquerade as user input."""

from core.llm.context_adapter import AgentScopeContextAdapter, render_session_input
from core.llm.context_ir import SESSION_CONTEXT_META_KEY
from core.llm.message_compat import session_to_msgs
from orchestration.subagents.plan_mode import _build_plan_generation_messages
from orchestration.workflow import _build_skill_injection


def test_plan_generation_policy_attachment_and_task_keep_distinct_provenance():
    rows = _build_plan_generation_messages(
        "PLAN-SYSTEM-RULE",
        "REAL-USER-TASK",
        "FILE-EVIDENCE",
    )
    messages = session_to_msgs(rows)
    items = AgentScopeContextAdapter().items_from_messages(messages)

    assert [item.kind for item in items] == [
        "system_rule",
        "attachment",
        "user_input",
    ]
    assert [(item.origin, item.trust) for item in items] == [
        ("harness:plan_generation", "system"),
        ("user:uploaded_files", "user"),
        ("user:chat", "user"),
    ]
    assert messages[-1].get_text_content() == "用户任务：REAL-USER-TASK"


def test_explicit_skill_capability_hint_is_a_system_reminder():
    row = _build_skill_injection(
        {
            "mcp_ids": ["search"],
            "plugin_name": "Research",
            "chat_mode": "fast",
        }
    )

    assert row is not None
    item = AgentScopeContextAdapter().items_from_messages(session_to_msgs([row]))[0]
    assert item.kind == "reminder"
    assert item.origin == "harness:explicit_invocation"
    assert item.trust == "system"


def test_pending_system_wake_remains_a_reminder_when_rendered_as_live_input():
    row = {
        "role": "user",
        "content": "wake the agent",
        SESSION_CONTEXT_META_KEY: {
            "kind": "reminder",
            "origin": "harness:job_wakeup:job_finish",
            "trust": "system",
            "visibility": "model",
            "priority": 950,
            "token_budget": 4_000,
            "truncation_policy": "never",
            "cache_class": "dynamic",
            "render_role": "user",
        },
    }

    message = render_session_input(row, created_seq=65_000)
    item = AgentScopeContextAdapter().items_from_messages([message])[0]

    assert item.kind == "reminder"
    assert item.origin == "harness:job_wakeup:job_finish"
    assert item.trust == "system"
