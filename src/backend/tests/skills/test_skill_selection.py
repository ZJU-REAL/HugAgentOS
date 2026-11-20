"""select_skills_for_query candidate filtering and its no-model fallback."""

from __future__ import annotations

import pytest

from core.capabilities.skills.registry import AgentSkillMetadata
from core.llm import skill_selection
from core.llm.skill_selection import select_skills_for_query


class _StubLoader:
    def __init__(self, ids):
        self._metadata = {
            sid: AgentSkillMetadata(id=sid, name=sid, description=f"{sid} skill", version="1")
            for sid in ids
        }

    def load_all_metadata(self):
        return self._metadata


@pytest.fixture(autouse=True)
def stub_loader(monkeypatch):
    loader = _StubLoader(["c-skill", "a-skill", "b-skill"])
    monkeypatch.setattr(skill_selection, "get_skill_loader", lambda: loader)
    return loader


def test_without_model_returns_sorted_available_and_enabled_skills():
    selected = select_skills_for_query(
        user_query="请梳理这个事项的申报流程",
        available_skill_ids=["c-skill", "a-skill", "b-skill"],
        enabled_skill_ids=["a-skill", "c-skill"],
        model=None,
    )
    assert selected == ["a-skill", "c-skill"]


def test_without_model_respects_max_skills():
    selected = select_skills_for_query(
        user_query="q",
        available_skill_ids=["c-skill", "a-skill", "b-skill"],
        enabled_skill_ids=["a-skill", "b-skill", "c-skill"],
        model=None,
        max_skills=2,
    )
    assert selected == ["a-skill", "b-skill"]


def test_skills_unknown_to_the_loader_are_dropped():
    selected = select_skills_for_query(
        user_query="q",
        available_skill_ids=["a-skill", "ghost"],
        enabled_skill_ids=["a-skill", "ghost"],
        model=None,
    )
    assert selected == ["a-skill"]


@pytest.mark.parametrize(
    ("available", "enabled"),
    [([], ["a-skill"]), (["a-skill"], []), (["a-skill"], ["b-skill"]), (["ghost"], ["ghost"])],
)
def test_no_candidates_returns_empty_without_calling_model(available, enabled):
    def _model(**_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("model must not be called without candidates")

    assert (
        select_skills_for_query(
            user_query="q", available_skill_ids=available, enabled_skill_ids=enabled, model=_model
        )
        == []
    )
