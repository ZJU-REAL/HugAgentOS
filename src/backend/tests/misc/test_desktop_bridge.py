"""混合架构 P2：桌面壳 → 本机后端身份桥接（core.auth.desktop_bridge）。

桥接语义：秘密匹配 + 用户头合法 → 按云端 user_center_id get-or-create 本机
shadow 用户并授予 CE 管理能力；任何不满足都静默 None（回落常规认证）。
"""

from __future__ import annotations

import base64
import json

from core.auth import desktop_bridge


class _Req:
    """resolve_bridge_user 只用 request.headers.get()，一个 dict 壳即可。"""

    def __init__(self, headers=None):
        self.headers = headers or {}


def _user_header(**overrides) -> str:
    payload = {"user_center_id": "cloud_user_42", "username": "aaron", **overrides}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_disabled_without_env(db_session, monkeypatch):
    monkeypatch.delenv(desktop_bridge.BRIDGE_SECRET_ENV, raising=False)
    req = _Req({desktop_bridge.BRIDGE_SECRET_HEADER: "whatever"})
    assert desktop_bridge.resolve_bridge_user(req, db_session) is None


def test_secret_mismatch_falls_through(db_session, monkeypatch):
    monkeypatch.setenv(desktop_bridge.BRIDGE_SECRET_ENV, "s3cret")
    req = _Req(
        {
            desktop_bridge.BRIDGE_SECRET_HEADER: "wrong",
            desktop_bridge.BRIDGE_USER_HEADER: _user_header(),
        }
    )
    assert desktop_bridge.resolve_bridge_user(req, db_session) is None


def test_malformed_user_header_falls_through(db_session, monkeypatch):
    monkeypatch.setenv(desktop_bridge.BRIDGE_SECRET_ENV, "s3cret")
    req = _Req(
        {
            desktop_bridge.BRIDGE_SECRET_HEADER: "s3cret",
            desktop_bridge.BRIDGE_USER_HEADER: "not-base64!!",
        }
    )
    assert desktop_bridge.resolve_bridge_user(req, db_session) is None


def test_bridge_seeds_cloud_user_with_admin_caps(db_session, monkeypatch):
    monkeypatch.setenv(desktop_bridge.BRIDGE_SECRET_ENV, "s3cret")
    req = _Req(
        {
            desktop_bridge.BRIDGE_SECRET_HEADER: "s3cret",
            desktop_bridge.BRIDGE_USER_HEADER: _user_header(),
        }
    )
    ctx = desktop_bridge.resolve_bridge_user(req, db_session)
    assert ctx is not None
    assert ctx.user_center_id == "cloud_user_42"
    assert ctx.username == "aaron"

    from core.db.models import UserShadow

    shadow = (
        db_session.query(UserShadow)
        .filter(UserShadow.user_center_id == "cloud_user_42")
        .first()
    )
    assert shadow is not None
    meta = shadow.extra_data or {}
    # 本机实例的机器主人：管理能力已 seed，且不触发首启向导/改密。
    assert meta.get("can_system_config") is True
    assert meta.get("desktop_bridge_admin") is True
    assert meta.get("auth_source") == "desktop_bridge"
    assert "must_change_password" not in meta
    assert "onboarding_required" not in meta


def test_bridge_is_idempotent_same_identity(db_session, monkeypatch):
    monkeypatch.setenv(desktop_bridge.BRIDGE_SECRET_ENV, "s3cret")
    req = _Req(
        {
            desktop_bridge.BRIDGE_SECRET_HEADER: "s3cret",
            desktop_bridge.BRIDGE_USER_HEADER: _user_header(),
        }
    )
    first = desktop_bridge.resolve_bridge_user(req, db_session)
    second = desktop_bridge.resolve_bridge_user(req, db_session)
    assert first is not None and second is not None
    assert first.user_id == second.user_id

    from core.db.models import UserShadow

    count = (
        db_session.query(UserShadow)
        .filter(UserShadow.user_center_id == "cloud_user_42")
        .count()
    )
    assert count == 1
