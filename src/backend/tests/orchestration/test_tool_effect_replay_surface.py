"""Tool-effect replay rebuilds the run's whole tool surface, not its explicit invocations."""
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_replay_uses_enabled_surface_not_turn_invocations(monkeypatch):
    import core.runs.tool_effect_recovery as tool_effect_recovery

    context = {
        "enabled_mcps": ["web_search", "automation_task"],
        "enabled_skills": ["report-skill"],
        "enabled_kbs": ["kb-1"],
        # This turn explicitly invoked one connector; it is already part of enabled_mcps.
        "mcp_ids": ["web_search"],
    }

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, *a):
            return SimpleNamespace(
                recovery_snapshot={"worker_args": {"context": context}},
                request_payload={},
                user_id="owner",
                chat_id="chat",
            )

    made = []

    class Built(Exception):
        pass

    async def create(**kw):
        made.append(kw)
        raise Built

    monkeypatch.setattr("core.db.engine.SessionLocal", Db)
    monkeypatch.setattr("core.harness.registry.run_capabilities.has_run_capabilities", lambda: False)
    monkeypatch.setattr("core.llm.factory.create_agent_executor", create)
    intent = SimpleNamespace(
        run_id="run", tool_name="create_scheduled_task", tool_call_id="call",
        effect_id="effect", redacted_args={},
    )
    with pytest.raises(Built):
        await tool_effect_recovery.replay_tool_intent(intent)
    assert made[0]["enabled_mcp_ids"] == ["web_search", "automation_task"]
    assert made[0]["enabled_skill_ids"] == ["report-skill"]
    assert made[0]["enabled_kb_ids"] == ["kb-1"]
