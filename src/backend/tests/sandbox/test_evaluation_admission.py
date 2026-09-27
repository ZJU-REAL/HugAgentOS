"""Lease admission precedes private-context resolution and never degrades."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.schemas import ChatRequest
from core.infra.ephemeral import LocalEphemeralState
from core.sandbox import evaluation_binding as binding
from core.services import evaluation_admission as admission


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    state = LocalEphemeralState()
    monkeypatch.setattr(binding, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(admission, "settings", SimpleNamespace(sandbox=SimpleNamespace(provider="opensandbox")))


def request(chat_id, **updates):
    # Require real schema names so a misspelled guard cannot pass this suite.
    assert set(updates) <= ChatRequest.model_fields.keys()
    return ChatRequest(chat_id=chat_id, message="Solve this task").model_copy(update=updates)


async def test_ordinary_request_bypasses_evaluation_admission(monkeypatch):
    activate = AsyncMock(side_effect=AssertionError("ordinary request touched evaluation state"))
    monkeypatch.setattr(binding, "activate", activate)
    monkeypatch.setattr(admission.settings.sandbox, "provider", "script_runner")
    await admission.authorize(request("ordinary", project_id="personal", enabled_mcps=["private"]), "owner")
    activate.assert_not_awaited()


async def test_valid_owner_activates_exact_binding():
    item = await binding.create("owner", "container", 60)
    await admission.authorize(request(item.session_id), "owner")
    current = await binding.get(item.session_id)
    assert current.phase == "active"
    assert current.sandbox_id == "container"
    assert current.owner_user_id == "owner"


@pytest.mark.parametrize("updates", [
    {"attachments": ["private"]}, {"project_id": "private"},
    {"referenced_chats": [{"chat_id": "private"}]}, {"quoted_follow_up": {"text": "private"}},
    {"enabled_mcps": ["private"]}, {"enabled_skills": ["private"]},
    {"enabled_kbs": ["private"]}, {"enabled_agents": ["private"]},
    {"skill_id": "private"}, {"connector_id": "private"}, {"plugin_id": "private"},
    {"mention_agent_id": "private"}, {"mention_name": "Private Agent"},
    {"plan_chat": True}, {"batch_chat": True}, {"workflow_chat": True}, {"site_chat": True},
])
async def test_private_context_is_rejected_before_activation(updates):
    item = await binding.create("owner", "container", 60)
    with pytest.raises(HTTPException) as error:
        await admission.authorize(request(item.session_id, **updates), "owner")
    assert error.value.status_code == 403
    assert error.value.detail == "evaluation_personal_context_forbidden"
    assert (await binding.get(item.session_id)).phase == "ready"


async def test_agent_scoped_key_is_rejected():
    item = await binding.create("owner", "container", 60)
    with pytest.raises(HTTPException) as error:
        await admission.authorize(request(item.session_id), "owner", agent_scoped=True)
    assert error.value.status_code == 403
    assert (await binding.get(item.session_id)).phase == "ready"


async def test_foreign_owner_cannot_activate_existing_binding():
    item = await binding.create("owner", "container", 60)
    with pytest.raises(HTTPException) as error:
        await admission.authorize(request(item.session_id), "intruder")
    assert error.value.status_code == 409
    assert error.value.detail == "evaluation_binding_unavailable"
    assert (await binding.get(item.session_id)).phase == "ready"


@pytest.mark.parametrize("phase", ["freezing", "frozen", "closed"])
async def test_non_writable_phase_is_rejected(phase):
    item = await binding.create("owner", "container", 60)
    if phase == "closed":
        await binding.close(item.session_id, "owner")
    else:
        await binding.begin_freeze(item.session_id, "owner")
        if phase == "frozen":
            await binding.finish_freeze(item.session_id, "owner")
    with pytest.raises(HTTPException) as error:
        await admission.authorize(request(item.session_id), "owner")
    assert error.value.status_code == 409
    assert (await binding.get(item.session_id)).phase == phase


@pytest.mark.parametrize("session", ["eval_" + "0" * 32, "eval_malformed"])
async def test_missing_or_malformed_lease_does_not_create_one(session):
    with pytest.raises(HTTPException) as error:
        await admission.authorize(request(session), "owner")
    assert error.value.status_code == 409


async def test_expired_lease_is_rejected(monkeypatch):
    item = await binding.create("owner", "container", 60)
    monkeypatch.setattr(binding.time, "time", lambda: item.expires_at + 1)
    with pytest.raises(HTTPException) as error:
        await admission.authorize(request(item.session_id), "owner")
    assert error.value.status_code == 409


async def test_other_provider_cannot_activate_lease(monkeypatch):
    item = await binding.create("owner", "container", 60)
    monkeypatch.setattr(admission.settings.sandbox, "provider", "script_runner")
    with pytest.raises(HTTPException) as error:
        await admission.authorize(request(item.session_id), "owner")
    assert error.value.status_code == 409
    assert error.value.detail == "evaluation_requires_opensandbox"
    assert (await binding.get(item.session_id)).phase == "ready"
