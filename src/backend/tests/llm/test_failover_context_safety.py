"""Budget admission follows the endpoint actually being called."""

import pytest
from agentscope.message import UserMsg
from agentscope.model import ChatResponse
from core.models import failover
from core.llm.context.safety import ContextBudgetExceeded
from core.llm.runtime.manifest_agent import ManifestBoundAgent
from core.llm.tools.ontology_toolkit import OntologyFilteredToolkit
from tests.llm.test_model_failover import _build, _Candidate, _status


@pytest.mark.asyncio
@pytest.mark.parametrize("cooling", [False, True])
async def test_smaller_failover_window_is_checked_before_dispatch(cooling):
    failover.clear_cooldowns()
    primary = _Candidate("large", ("raise", _status(402)), stream=False)
    primary.context_size = 32000
    backup = _Candidate("small", ("value", ChatResponse(content=[], is_last=True)), stream=False)
    backup.context_size = 100
    model = _build(primary, backup)
    if cooling:
        failover.mark_provider_down("large")
    agent = ManifestBoundAgent(
        name="probe",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )
    try:
        with pytest.raises(ContextBudgetExceeded):
            await agent.reply(UserMsg(name="user", content="x" * 10000))
        assert backup.calls == 0
        assert primary.calls == (0 if cooling else 1)
        assert agent.request_context_manifest["budget_details"]["context_window"] == 100
    finally:
        failover.clear_cooldowns()


def test_failover_instances_do_not_share_active_candidate():
    first = _build(_Candidate("first", ("value", None)))
    second = _build(_Candidate("second", ("value", None)))
    narrow = _Candidate("narrow", ("value", None))
    narrow.context_size = 100
    first._adopt(narrow)
    assert first.context_size == 100
    assert second.model == "model-second"
    assert second.context_size == 32000
