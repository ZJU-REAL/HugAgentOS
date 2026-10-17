"""Personal execution does not inherit chat turn budgets."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.llm.factory.request import AgentRequest
from core.llm.factory.runtime.execution_budget import execution_budget


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_name", ["personal_agent", "personal_agent_front", None])
async def test_personal_turns_ignore_explicit_chat_budgets(profile_name, monkeypatch):
    monkeypatch.setenv("CHAT_MAIN_MAX_ITERS", "3")
    manifest = Mock()
    result = await execution_budget(
        AgentRequest(profile=profile_name, max_iters=30),
        _capability_notices=[],
        _elapsed=lambda: 0,
        _log=Mock(),
        _manifest_builder=manifest,
        _turbo_code_exec=False,
        default_model=SimpleNamespace(context_size=128000, model="test"),
        profile=SimpleNamespace(max_react_turns=6, version=1, profile_id="test"),
        system_prompt="base",
    )
    if profile_name:
        assert result[1] == sys.maxsize
        assert result[4] == "base"
        manifest.add_prompt_section.assert_not_called()
    else:
        assert result[1] == 30
        manifest.add_prompt_section.assert_called_once()
