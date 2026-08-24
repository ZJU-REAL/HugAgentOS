"""Skill-selector model calls use the canonical Context IR adapter."""

from types import SimpleNamespace

from core.agent_skills.registry import AgentSkillMetadata
from core.agent_skills.selector import _llm_select_skills


class CaptureSelectorModel:
    context_size = 4_096

    def __init__(self):
        self.messages = None

    async def __call__(self, *, messages):  # noqa: ANN001, ANN202
        self.messages = messages
        return SimpleNamespace(content=[{"type": "text", "text": '["analysis"]'}])


def test_selector_messages_carry_context_ir_provenance():
    model = CaptureSelectorModel()
    skill = AgentSkillMetadata(
        id="analysis",
        name="Analysis",
        description="Analyze documents",
        version="1",
    )

    selected = _llm_select_skills(
        user_query="analyze this",
        candidates=[skill],
        model=model,
        max_skills=1,
    )

    assert selected == ["analysis"]
    assert [message.role for message in model.messages] == ["system", "user"]
    assert model.messages[0].metadata["harness_context_items"][0]["kind"] == "system_rule"
    assert model.messages[1].metadata["harness_context_items"][0]["kind"] == "user_input"
    assert model.messages[1].metadata["harness_context_items"][0]["origin"] == (
        "harness:skill_selector_request"
    )
