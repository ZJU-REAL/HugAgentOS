"""Default chat mode and the mode-switching permission (``core/chat/mode_policy.py``)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import capabilities
from core.auth.capabilities import BOOL_CAPABILITY_DEFAULTS, resolve_capabilities
from core.chat import mode_policy
from core.chat.modes import ChatModeService
from core.config.store import intent_router_options
from core.infra.exceptions import BadRequestError


# ── Resolution ────────────────────────────────────────────────────────────────
def test_switching_is_on_by_default_and_no_default_mode():
    assert BOOL_CAPABILITY_DEFAULTS["can_switch_chat_mode"] is True
    caps = resolve_capabilities({}, {}, {})
    assert caps["can_switch_chat_mode"] is True
    assert caps["default_chat_mode"] is None


def test_default_mode_falls_through_personal_role_team():
    assert resolve_capabilities({}, {}, {"default_chat_mode": "turbo"})["default_chat_mode"] == "turbo"
    assert (
        resolve_capabilities({}, {"default_chat_mode": "auto"}, {"default_chat_mode": "turbo"})[
            "default_chat_mode"
        ]
        == "auto"
    )
    assert (
        resolve_capabilities({"default_chat_mode": "standard"}, {"default_chat_mode": "auto"}, {})[
            "default_chat_mode"
        ]
        == "standard"
    )


def test_invalid_default_modes_are_ignored():
    for junk in ("", "  ", 3, True, "has space", "x" * 65, "../etc"):
        assert resolve_capabilities({"default_chat_mode": junk})["default_chat_mode"] is None


# ── Effective default (what the user can actually run) ───────────────────────
@pytest.fixture
def modes_db(db_session):
    ChatModeService(db_session).ensure_builtins()
    return db_session


@pytest.fixture
def router_enabled(monkeypatch):
    state = {"enabled": True}
    monkeypatch.setattr(
        intent_router_options,
        "intent_router_options",
        lambda: intent_router_options.IntentRouterOptions(enabled=state["enabled"]),
    )
    return state


@pytest.mark.parametrize(
    "configured, router_cap, expected",
    [
        (None, False, "standard"),
        ("turbo", False, "turbo"),
        ("gone", False, "standard"),
        ("auto", True, "auto"),
        ("auto", False, "standard"),
    ],
)
def test_effective_default_degrades_to_standard(modes_db, router_enabled, configured, router_cap, expected):
    caps = {"default_chat_mode": configured, "can_use_intent_router": router_cap}
    assert mode_policy.effective_default_slug(modes_db, "u1", caps) == expected


def test_router_default_needs_the_system_switch(modes_db, router_enabled):
    router_enabled["enabled"] = False
    caps = {"default_chat_mode": "auto", "can_use_intent_router": True}
    assert mode_policy.effective_default_slug(modes_db, "u1", caps) == "standard"


def test_validate_default_mode(modes_db):
    assert mode_policy.validate_default_mode(modes_db, None) is None
    assert mode_policy.validate_default_mode(modes_db, " ") is None
    assert mode_policy.validate_default_mode(modes_db, "auto") == "auto"
    assert mode_policy.validate_default_mode(modes_db, "turbo") == "turbo"
    with pytest.raises(BadRequestError):
        mode_policy.validate_default_mode(modes_db, "does-not-exist")


def test_private_modes_cannot_be_assigned(modes_db):
    from core.db.models.chat_mode import ChatMode

    row = ChatMode(id="mode_mine1", slug="mine1", name="我的", owner_user_id="u1", enabled=True)
    modes_db.add(row)
    modes_db.commit()
    with pytest.raises(BadRequestError):
        mode_policy.validate_default_mode(modes_db, "mine1")


# ── Server-side lock ─────────────────────────────────────────────────────────
def _caps(monkeypatch, **values):
    base = {"can_switch_chat_mode": True, "default_chat_mode": None, "can_use_intent_router": False}
    monkeypatch.setattr(capabilities, "resolve_user_capabilities", lambda db, uid: {**base, **values})


@pytest.mark.parametrize("requested", ["standard", "auto", "", None, "mine1"])
def test_locked_user_is_held_to_the_default_mode(modes_db, monkeypatch, requested):
    _caps(monkeypatch, can_switch_chat_mode=False, default_chat_mode="turbo")
    request = SimpleNamespace(mode_slug=requested)
    mode_policy.apply_mode_policy(modes_db, request, "u1")
    assert request.mode_slug == "turbo"


def test_locked_user_without_default_gets_standard(modes_db, monkeypatch):
    _caps(monkeypatch, can_switch_chat_mode=False)
    request = SimpleNamespace(mode_slug="turbo")
    mode_policy.apply_mode_policy(modes_db, request, "u1")
    assert request.mode_slug == "standard"


@pytest.mark.parametrize("requested", ["turbo", "auto", None])
def test_users_who_may_switch_keep_their_choice(modes_db, monkeypatch, requested):
    _caps(monkeypatch, default_chat_mode="turbo")
    request = SimpleNamespace(mode_slug=requested)
    mode_policy.apply_mode_policy(modes_db, request, "u1")
    assert request.mode_slug == requested


def test_policy_lookup_failure_leaves_the_request_alone(modes_db, monkeypatch):
    def boom(db, uid):
        raise RuntimeError("db down")

    monkeypatch.setattr(capabilities, "resolve_user_capabilities", boom)
    request = SimpleNamespace(mode_slug="turbo")
    mode_policy.apply_mode_policy(modes_db, request, "u1")
    assert request.mode_slug == "turbo"


def test_admission_applies_the_lock_before_project_init(modes_db, monkeypatch):
    """``_resolve_targets`` runs the policy first, so /init can still force 标准模式."""
    from core.chat.operations import admission

    calls = []
    monkeypatch.setattr(
        admission.chat_mode_policy, "apply_mode_policy", lambda db, req, uid: calls.append("policy")
    )

    def stop(*_args, **_kwargs):
        calls.append("targets")
        raise RuntimeError("stop here")

    monkeypatch.setattr(admission.chat_agent_targets, "_resolve_chat_agent_targets", stop)
    with pytest.raises(RuntimeError, match="stop here"):
        admission._resolve_targets(modes_db, SimpleNamespace(mode_slug="turbo"), "u1", None)
    assert calls == ["policy", "targets"]


# ── GET /v1/chat-modes carries the policy ────────────────────────────────────
@pytest.fixture
def client(modes_db, monkeypatch):
    from api.routes.v1.chat_modes import router
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from core.db.models import UserShadow

    modes_db.add(UserShadow(user_id="u1", user_center_id="u1", username="u1"))
    modes_db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: modes_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u1", user_center_id="u1", username="u1"
    )
    monkeypatch.setattr(intent_router_options, "intent_router_enabled", lambda: False)
    return TestClient(app)


def test_menu_defaults_to_standard_and_switchable(client):
    data = client.get("/v1/chat-modes").json()["data"]
    assert data["default_slug"] == "standard"
    assert data["can_switch"] is True


def test_menu_reports_a_locked_default(client, monkeypatch):
    _caps(monkeypatch, can_switch_chat_mode=False, default_chat_mode="turbo")
    data = client.get("/v1/chat-modes").json()["data"]
    assert data["default_slug"] == "turbo"
    assert data["can_switch"] is False
    assert {"standard", "turbo"} <= {m["slug"] for m in data["modes"]}


def test_assignable_modes_follow_the_config_guard(client):
    """The 默认模式 picker uses require_config (not the token-only /official), so a
    console opened by a session with 系统配置 can load it."""
    from api.deps import require_config

    app = client.app
    assert client.get("/v1/chat-modes/assignable").status_code in (401, 403, 503)
    app.dependency_overrides[require_config] = lambda: None
    data = client.get("/v1/chat-modes/assignable").json()["data"]
    assert {"standard", "turbo"} <= {m["slug"] for m in data["modes"]}
    assert all(set(m) == {"slug", "name"} for m in data["modes"])
