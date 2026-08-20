"""Regression tests for Lark auth-status parsing.

Two defects are pinned here, both observed on a real deployment where the
connection panel claimed 已登录 for two months while the sandbox correctly
reported "User identity: missing (refresh token expired)":

1. The backend invoked ``lark-cli auth status --format json``. No shipped CLI
   accepts that flag (1.0.34 offers only ``--verify``, 1.0.78 spells it
   ``--json``), so every call returned an "unknown flag" error instead of the
   status — identity backfill produced NULLs and the liveness probe could never
   reconcile a stale ``connected`` row.
2. ``is_authenticated`` treated "an openId/userName appears somewhere in the
   JSON" as proof of login. Those fields survive token expiry, so an expired
   login still read as authenticated.

The payloads below are captured verbatim from lark-cli (trimmed for length).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.services import lark_service as ls

_EXPIRED_STATUS = json.dumps(
    {
        "appId": "cli_a9427954a961dbc2",
        "brand": "feishu",
        "defaultAs": "auto",
        "expiresAt": "2026-06-24T12:34:11+08:00",
        "grantedAt": "2026-06-18T14:51:52+08:00",
        "identities": {
            "bot": {"status": "ready", "available": True, "message": "Bot identity: ready"},
            "user": {
                "status": "missing",
                "available": False,
                "message": "User identity: missing (refresh token expired)",
                "openId": "ou_ed18141f5664973c624e5341f262b175",
                "userName": "Aaron Zhu",
                "tokenStatus": "expired",
                "expiresAt": "2026-06-24T12:34:11+08:00",
                "refreshExpiresAt": "2026-07-01T10:34:11+08:00",
            },
        },
    }
)

_READY_STATUS = json.dumps(
    {
        "appId": "cli_a9427954a961dbc2",
        "brand": "feishu",
        "identities": {
            "bot": {"status": "ready", "available": True},
            "user": {
                "status": "ready",
                "available": True,
                "openId": "ou_ed18141f5664973c624e5341f262b175",
                "userName": "Aaron Zhu",
                "tenantKey": "tk_demo",
                "tokenStatus": "valid",
                "expiresAt": "2099-01-01T00:00:00+08:00",
            },
        },
    }
)

# ``auth login --no-wait --json`` — no ``identities`` block at all.
_DEVICE_LOGIN = json.dumps(
    {
        "deviceCode": "dc_abc",
        "userCode": "WXYZ-1234",
        "verificationUrl": "https://example.invalid/device",
        "verificationUrlComplete": "https://example.invalid/device?code=WXYZ-1234",
    }
)


# -- Structured user-identity verdict ------------------------------------


def test_expired_user_identity_is_not_authenticated():
    """The crux: openId/userName are still present, but the login is dead."""
    assert ls.parse_user_identity_state(_EXPIRED_STATUS) is False
    assert ls.is_authenticated(_EXPIRED_STATUS) is False


def test_ready_user_identity_is_authenticated():
    assert ls.parse_user_identity_state(_READY_STATUS) is True
    assert ls.is_authenticated(_READY_STATUS) is True


def test_unrecognized_shape_falls_back_to_the_lenient_heuristics():
    """Device-flow output carries no ``identities``; the old path must still serve it."""
    assert ls.parse_user_identity_state(_DEVICE_LOGIN) is None
    assert ls.parse_user_identity_state("not json at all") is None
    assert ls.parse_user_identity_state(json.dumps({"authenticated": True})) is None
    assert ls.is_authenticated(json.dumps({"authenticated": True})) is True


# -- The CLI is never handed a flag it does not accept -------------------


def _record_run(monkeypatch, stdout):
    calls = []

    async def _fake_run(_user_id, args, **_kw):
        calls.append(list(args))
        return stdout, "", 0

    monkeypatch.setattr(ls, "_run_lark", _fake_run)
    return calls


def test_verify_and_refresh_never_passes_format_json(monkeypatch):
    calls = _record_run(monkeypatch, _READY_STATUS)

    assert asyncio.run(ls.verify_and_refresh("u1")) == "valid"

    assert calls == [["auth", "status", "--verify"]]
    assert all("--format" not in c for c in calls)


def test_verify_and_refresh_reports_expired_login_as_invalid(monkeypatch):
    _record_run(monkeypatch, _EXPIRED_STATUS)

    assert asyncio.run(ls.verify_and_refresh("u1")) == "invalid"


def test_verify_and_refresh_stays_unknown_on_timeout(monkeypatch):
    async def _timeout(_user_id, _args, **_kw):
        return "", "lark-cli auth status timeout", 124

    monkeypatch.setattr(ls, "_run_lark", _timeout)

    assert asyncio.run(ls.verify_and_refresh("u1")) == "unknown"


def test_build_connected_update_backfills_identity(monkeypatch):
    """With the bogus flag gone, open_id / name finally land in the DB row."""
    calls = _record_run(monkeypatch, _READY_STATUS)

    data = asyncio.run(ls._build_connected_update("u1"))

    assert calls == [["auth", "status"]]
    assert data["status"] == "connected"
    assert data["lark_open_id"] == "ou_ed18141f5664973c624e5341f262b175"
    assert data["lark_name"] == "Aaron Zhu"


# -- Login completion must not mark connected on exit-code alone ---------


def _drive_device_flow(monkeypatch, status_stdout, login_rc=0):
    captured = {}

    async def _fake_run(_user_id, args, **_kw):
        if args[:2] == ["auth", "login"]:
            return json.dumps({"ok": True}), "", login_rc
        return status_stdout, "", 0

    monkeypatch.setattr(ls, "_run_lark", _fake_run)
    monkeypatch.setattr(ls, "_update_connection", lambda uid, data: captured.update(data))
    asyncio.run(ls._device_complete_flow("u1", "dc_abc"))
    return captured


def test_login_exit_zero_without_a_token_is_not_connected(monkeypatch):
    """rc==0 used to be enough — that is how rows became ``connected`` with a
    NULL identity and no token file on disk."""
    captured = _drive_device_flow(monkeypatch, _EXPIRED_STATUS)

    assert captured["status"] == "error"


def test_login_with_a_real_token_is_connected(monkeypatch):
    captured = _drive_device_flow(monkeypatch, _READY_STATUS)

    assert captured["status"] == "connected"
    assert captured["lark_open_id"] == "ou_ed18141f5664973c624e5341f262b175"


@pytest.mark.parametrize("stdout", ["", "some unparseable banner"])
def test_inconclusive_status_keeps_the_lenient_exit_zero_path(monkeypatch, stdout):
    """Unfamiliar CLI output must not start failing otherwise-successful logins."""
    captured = _drive_device_flow(monkeypatch, stdout)

    assert captured["status"] == "connected"
