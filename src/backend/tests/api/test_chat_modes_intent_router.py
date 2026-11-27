"""GET /v1/chat-modes 在开关与权限都满足时把「智能路由」虚拟项放在最前面。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.chat import mode_router
from core.config.store import intent_router_options


@pytest.fixture
def client(db_session, monkeypatch):
    from api.routes.v1.chat_modes import router
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db

    from core.db.models import UserShadow

    db_session.add(UserShadow(user_id="u1", user_center_id="u1", username="u1"))
    db_session.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u1", user_center_id="u1", username="u1"
    )
    state = {"enabled": True, "allowed": True}
    monkeypatch.setattr(intent_router_options, "intent_router_enabled", lambda: state["enabled"])
    monkeypatch.setattr(mode_router, "user_allowed", lambda db, uid: state["allowed"])
    return TestClient(app), state


def _modes(client):
    resp = client.get("/v1/chat-modes")
    assert resp.status_code == 200
    return resp.json()["data"]["modes"]


def test_router_entry_is_inserted_first(client):
    http, _ = client
    modes = _modes(http)
    first, rest = modes[0], modes[1:]
    assert first == {
        "slug": "auto",
        "name": "智能路由",
        "description": "每次提问自动识别意图，进入合适的模式",
        "is_builtin": True,
        "is_private": False,
        "default_effort": "fast",
        "effort_locked": False,
        "is_router": True,
    }
    assert rest and {"standard", "turbo"} <= {m["slug"] for m in rest}
    # 与真实模式的字段一致，只多一个 is_router
    assert set(first) - set(rest[0]) == {"is_router"}
    assert not any(m.get("is_router") for m in rest)


@pytest.mark.parametrize("enabled, allowed", [(False, True), (True, False)])
def test_router_entry_needs_switch_and_capability(client, enabled, allowed):
    http, state = client
    state.update(enabled=enabled, allowed=allowed)
    assert "auto" not in [m["slug"] for m in _modes(http)]


def test_capability_flag_is_served_and_off_by_default():
    from core.auth.capabilities import BOOL_CAPABILITY_DEFAULTS

    assert BOOL_CAPABILITY_DEFAULTS["can_use_intent_router"] is False


def test_contract_registrations():
    from core.llm.prompt_kinds import KIND_SPECS
    from core.llm.prompt_files import read_fs_parts
    from core.models.repository import ROLE_DEFINITIONS
    from core.config.store.system_config_constants import SEED_CONFIGS

    assert KIND_SPECS["intent_router"].layout == "single"
    assert KIND_SPECS["intent_router"].label == "用户意图识别"
    body = "".join(p["content"] for p in read_fs_parts("intent_router"))
    assert "智能保持" in body and "mode_slug" in body
    assert ROLE_DEFINITIONS["intent_router"] == {"label": "用户意图识别", "type": "chat"}
    seeds = {key: default for key, default, *_ in SEED_CONFIGS if key.startswith("intent_router.")}
    assert seeds == {
        "intent_router.enable": "false",
        "intent_router.strategy": "smart_keep",
        "intent_router.candidate_slugs": "",
        "intent_router.fallback_slug": "standard",
        "intent_router.timeout_ms": "5000",
    }
