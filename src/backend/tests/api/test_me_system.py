"""Unit tests for personal system settings (/v1/me/system).

Covers:
- The three states of the `user_can_manage_system_settings` gate: super_admin
  capability bit / EE regular user denied / CE+mock single trust domain allowed
- Service config whitelist: out-of-bounds key 400, whitelisted key passes and triggers bulk_set
- Consistency between whitelist groups and SEED_CONFIGS groups (prevents the
  whitelist dangling after a seed regroup)
- models.py pricing gate: CE skips model_pricing reads/writes
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from core.db.models import UserShadow
from fastapi import HTTPException

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def super_admin(db_session):
    u = UserShadow(
        user_id="user_root",
        username="Root",
        extra_data={"role": "super_admin"},
    )
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture
def regular(db_session):
    u = UserShadow(user_id="user_plain", username="Plain", extra_data={})
    db_session.add(u)
    db_session.commit()
    return u


def _patch_edition(monkeypatch, *, edition: str, auth_mode: str):
    """deps' settings is a frozen dataclass; replace the module-level reference with a controllable fake object."""
    import api.deps as deps

    fake = SimpleNamespace(
        edition=SimpleNamespace(edition=edition),
        auth=SimpleNamespace(mode=auth_mode, config_token=""),
    )
    monkeypatch.setattr(deps, "settings", fake)


# ── Gate: user_can_manage_system_settings ───────────────────────────────────


def test_super_admin_allowed_regardless_of_edition(db_session, super_admin, monkeypatch):
    from api.deps import user_can_manage_system_settings

    _patch_edition(monkeypatch, edition="ee", auth_mode="session")
    assert user_can_manage_system_settings(db_session, super_admin.user_id) is True


def test_regular_user_denied_in_ee(db_session, regular, monkeypatch):
    from api.deps import user_can_manage_system_settings

    _patch_edition(monkeypatch, edition="ee", auth_mode="mock")
    assert user_can_manage_system_settings(db_session, regular.user_id) is False


def test_ce_mock_single_trust_domain_allows_any_user(db_session, regular, monkeypatch):
    from api.deps import user_can_manage_system_settings

    _patch_edition(monkeypatch, edition="ce", auth_mode="mock")
    assert user_can_manage_system_settings(db_session, regular.user_id) is True


def test_ce_with_real_auth_still_requires_capability(db_session, regular, monkeypatch):
    from api.deps import user_can_manage_system_settings

    _patch_edition(monkeypatch, edition="ce", auth_mode="session")
    assert user_can_manage_system_settings(db_session, regular.user_id) is False


def test_anonymous_denied(db_session, monkeypatch):
    from api.deps import user_can_manage_system_settings

    _patch_edition(monkeypatch, edition="ce", auth_mode="mock")
    assert user_can_manage_system_settings(db_session, None) is False


def _run(coro):
    """Reuse/create the current event loop to drive the coroutine, keeping the loop **open and current**.

    Do not use @pytest.mark.asyncio: pytest-asyncio's teardown corrupts the
    current loop state, polluting subsequent existing tests that use the old-style
    ``asyncio.get_event_loop()`` (tests/memory etc.); by the same reasoning, never
    close/clear the current loop here either.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ── Service config whitelist ─────────────────────────────────────────────────────────


class _FakeSvc:
    def __init__(self):
        self.written = None

    def get_all_configs(self):
        return [
            {"config_key": "internet_search.engine", "group_key": "internet_search"},
            {"config_key": "internet_search.tavily_api_key", "group_key": "internet_search"},
            {"config_key": "dingtalk.client_secret", "group_key": "dingtalk"},
        ]

    def bulk_set(self, items, updated_by="admin"):
        self.written = items


@pytest.fixture
def fake_svc(monkeypatch):
    import api.routes.v1.me_system as me_system

    svc = _FakeSvc()
    monkeypatch.setattr(me_system, "_svc", lambda: svc)
    # A successful PUT schedules an MCP pool rebuild; the test has no event loop, so replace it with a no-op coroutine factory
    monkeypatch.setattr(me_system.asyncio, "ensure_future", lambda coro: coro.close())
    return svc


def test_update_rejects_key_outside_whitelist(fake_svc):
    from api.routes.v1.me_system import BulkUpdateRequest, ConfigUpdateItem, update_personal_configs

    body = BulkUpdateRequest(items=[ConfigUpdateItem(key="dingtalk.client_secret", value="x")])
    with pytest.raises(HTTPException) as exc:
        _run(update_personal_configs(body, _="tester"))
    assert exc.value.status_code == 400
    assert fake_svc.written is None


def test_update_rejects_unknown_key(fake_svc):
    from api.routes.v1.me_system import BulkUpdateRequest, ConfigUpdateItem, update_personal_configs

    body = BulkUpdateRequest(items=[ConfigUpdateItem(key="nope.key", value="x")])
    with pytest.raises(HTTPException) as exc:
        _run(update_personal_configs(body, _="tester"))
    assert exc.value.status_code == 400


def test_update_accepts_whitelisted_keys(fake_svc):
    from api.routes.v1.me_system import BulkUpdateRequest, ConfigUpdateItem, update_personal_configs

    body = BulkUpdateRequest(
        items=[
            ConfigUpdateItem(key="internet_search.engine", value="baidu"),
            ConfigUpdateItem(key="internet_search.tavily_api_key", value="tvly-abc"),
        ]
    )
    resp = _run(update_personal_configs(body, _="tester"))
    assert resp["data"]["updated"] == 2
    assert {i["key"] for i in fake_svc.written} == {
        "internet_search.engine",
        "internet_search.tavily_api_key",
    }


def test_list_masks_secrets(monkeypatch):
    import api.routes.v1.me_system as me_system
    from api.routes.v1.me_system import list_personal_configs

    class _Svc:
        def get_all_configs(self):
            return [
                {
                    "config_key": "internet_search.tavily_api_key",
                    "config_value": "tvly-supersecret-123",
                    "group_key": "internet_search",
                    "is_secret": True,
                },
                {
                    "config_key": "dingtalk.client_secret",
                    "config_value": "should-not-appear",
                    "group_key": "dingtalk",
                    "is_secret": True,
                },
            ]

    monkeypatch.setattr(me_system, "_svc", lambda: _Svc())
    resp = _run(list_personal_configs(_="tester"))
    groups = {g["group_key"]: g for g in resp["data"]}
    assert "dingtalk" not in groups  # non-whitelisted groups do not appear
    items = groups["internet_search"]["items"]
    assert items and "****" in items[0]["config_value"]
    assert "supersecret" not in items[0]["config_value"]


def test_ce_excludes_shared_knowledge_base_connector(monkeypatch):
    import api.routes.v1.me_system as me_system

    monkeypatch.setattr(
        me_system,
        "settings",
        SimpleNamespace(edition=SimpleNamespace(edition="ce")),
    )
    assert "knowledge_base" not in me_system._personal_groups()
    assert "internet_search" in me_system._personal_groups()


def test_whitelist_groups_exist_in_seed_configs():
    """Every whitelist group must have config items in SEED_CONFIGS (prevents the whitelist dangling after a seed regroup)."""
    from api.routes.v1.me_system import PERSONAL_GROUPS
    from core.services.system_config import SEED_CONFIGS

    seed_groups = {group for _, _, _, _, group, _ in SEED_CONFIGS}
    missing = set(PERSONAL_GROUPS) - seed_groups
    assert not missing, f"白名单分组在 SEED_CONFIGS 中不存在: {missing}"
    # Enterprise groups must never enter the whitelist
    assert not ({"dingtalk", "lark", "auth", "industry"} & set(PERSONAL_GROUPS))


# ── models.py pricing gate by edition ────────────────────────────────────────────────


def test_pricing_disabled_in_ce(monkeypatch):
    import api.routes.v1.models as models

    fake = SimpleNamespace(edition=SimpleNamespace(edition="ce"))
    monkeypatch.setattr(models, "settings", fake)
    # CE: returns empty/None/no-op without touching the DB (passing None for db proves no query ran)
    assert models._pricing_map(None) == {}
    assert models._get_pricing(None, "gpt-x") is None
    models._upsert_pricing(None, model_name="gpt-x", input_price=1.0)


def test_pricing_enabled_in_ee(monkeypatch, db_session):
    import api.routes.v1.models as models

    fake = SimpleNamespace(edition=SimpleNamespace(edition="ee"))
    monkeypatch.setattr(models, "settings", fake)
    assert models._pricing_enabled() is True
    assert (
        models._pricing_map(db_session) == {}
    )  # runs a real query, empty table returns an empty map
