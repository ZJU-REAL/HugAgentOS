"""Personal Agent as an optional module: env switch, per-user permission, core seams."""
from types import SimpleNamespace

import pytest

from core.db.models import ChatSession, UserShadow
from core.db.reserved_chats import ordinary_chat_filter, reserved_chat_owner
from core.harness.registry import registry
from personal_agent import access
from personal_agent import extension
from personal_agent.context import AgentContext
from tests.test_personal_agent import sessions  # noqa: F401  (shared fixture)


@pytest.fixture
def empty_registry(monkeypatch):
    for name, value in (("_enabled", set()), ("_agent_profiles", {}), ("_sandbox_routes", []),
                        ("_channel_routes", []), ("_lifecycle", []), ("_routers", [])):
        monkeypatch.setattr(registry.registration_state(), name.lstrip("_"), value)


def grant(db, user_id, value=True):
    row = db.get(UserShadow, user_id)
    row.extra_data = {**(row.extra_data or {}), access.CAPABILITY: value}
    db.commit()


def test_disabled_module_registers_nothing(empty_registry, monkeypatch):
    monkeypatch.delenv("PERSONAL_AGENT_ENABLED", raising=False)
    assert extension.setup() is False
    assert registry.routers() == () and registry.lifecycle_hooks() == () and registry.channel_routes() == ()
    assert registry.sandbox_route("pa_box_x") is None
    with pytest.raises(ValueError):
        registry.agent_profile(access.NAME)


def test_enabled_module_registers_every_hook(empty_registry, monkeypatch):
    monkeypatch.setenv("PERSONAL_AGENT_ENABLED", "true")
    assert extension.setup() is True
    profile = registry.agent_profile(access.NAME)
    assert profile.tool_filter("pa_finish") and not profile.tool_filter("bash")
    assert profile.inherit_default_mcps is False
    assert registry.sandbox_route("pa_box_x") is not None and registry.sandbox_route("chat_x") is None
    assert len(registry.routers()) == 2 and all(feature is None for _, feature in registry.routers())
    assert len(registry.lifecycle_hooks()) == 1 and len(registry.channel_routes()) == 1


def test_access_needs_module_and_capability(sessions, empty_registry, monkeypatch):
    _edition(monkeypatch, "ee")
    with sessions() as db:
        assert access.allowed(db, "owner") is False  # module off
        registry.mark_enabled(access.NAME)
        assert access.allowed(db, "owner") is False  # no permission
        grant(db, "owner")
        assert access.allowed(db, "owner") is True
        assert access.visible({}, {access.CAPABILITY: True}) is True
        assert access.visible({"role": "super_admin"}, {}) is True
        assert access.visible({}, {}) is False


def test_revoked_user_stops_background_work(sessions, empty_registry, monkeypatch):
    _edition(monkeypatch, "ee")
    from personal_agent.channels import channel_bridge
    from personal_agent.scheduling import schedules
    from personal_agent.tasks import worker as module

    registry.mark_enabled(access.NAME)
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        grant(db, "owner")
        service = AgentContext(db, "owner")
        service.identity.ensure()
        service.tasks.submit("Prepare file", "request-one")
        schedules.create(db, "owner", "Call", "Call Bob", run_at="2099-01-01T09:00")
        agent = service.identity.agent()
        agent.config = {**agent.config, "channel_ids": ["ch1"]}
        db.commit()
        grant(db, "owner", False)
    conn = SimpleNamespace(channel_id="ch1", channel_type="lark", owner_user_id="owner")
    assert module.PersonalAgentWorker().claim() is None
    with sessions() as db:
        assert schedules.fire_due(db, now=4102444800 + 86400) == 0
        assert channel_bridge.handled_by_agent(db, conn) is False
        grant(db, "owner")
        assert channel_bridge.handled_by_agent(db, conn) is True
    assert module.PersonalAgentWorker().claim() is not None


def test_reserved_chats_stay_hidden_without_the_module(sessions, empty_registry):
    assert reserved_chat_owner("pa_chat_x") == "Personal Agent" and reserved_chat_owner("chat_x") is None
    with sessions() as db:
        db.add_all([ChatSession(chat_id="pa_chat_1", user_id="owner"), ChatSession(chat_id="chat_1", user_id="owner")])
        db.commit()
        visible = db.query(ChatSession.chat_id).filter(ordinary_chat_filter(ChatSession.chat_id)).all()
        assert [row.chat_id for row in visible] == ["chat_1"]


def test_reserved_chats_are_unreachable_through_ordinary_chat_access(sessions, empty_registry):
    # Every ordinary read (session detail, history, @-references, the history tool,
    # sidebar discovery) resolves chats through these lookups or the reference listing.
    from core.chat.references import list_referencable_sessions
    from core.chat.service import ChatService

    with sessions() as db:
        db.add_all([ChatSession(chat_id="pa_chat_1", user_id="owner"), ChatSession(chat_id="chat_1", user_id="owner")])
        db.commit()
        service = ChatService(db)
        assert service.get_session("pa_chat_1", "owner") is None
        assert service.get_session_with_access("pa_chat_1", "owner") is None
        assert service.get_session("chat_1", "owner") is not None
        assert service.get_session_with_access("chat_1", "owner") is not None
        assert [row.chat_id for row in list_referencable_sessions(db, "owner")] == ["chat_1"]


def test_community_module_registers_without_enterprise_license(empty_registry, monkeypatch):
    from core.config.settings import settings
    from dataclasses import replace
    monkeypatch.setitem(settings.__dict__, "edition", replace(settings.edition, edition="ce"))
    monkeypatch.setenv("PERSONAL_AGENT_ENABLED", "true")
    assert extension.setup() is True
    assert all(feature is None for _, feature in registry.routers())


def _edition(monkeypatch, edition):
    from core.config.settings import settings
    from dataclasses import replace
    monkeypatch.setitem(settings.__dict__, "edition", replace(settings.edition, edition=edition))


def test_community_access_requires_existing_user_and_enabled_module(sessions, empty_registry, monkeypatch):
    _edition(monkeypatch, "ce")
    with sessions() as db:
        assert not access.allowed(db, "owner")
        registry.mark_enabled(access.NAME)
        assert access.allowed(db, "owner")
        assert not access.allowed(db, "missing")
        assert not access.allowed(db, "")
        assert access.visible({}, {})


def test_history_filters_internal_wake_messages():
    from core.chat.operations.history import _is_internal_message
    assert not _is_internal_message(SimpleNamespace(role="user", content="Hello", extra_data={}))
    assert _is_internal_message(SimpleNamespace(role="user", content="[系统] 进度播报：running", extra_data={}))
    assert _is_internal_message(SimpleNamespace(role="assistant", content="hidden", extra_data={"hidden_in_chat": True}))
