"""Regression tests for OpenSandbox per-user volume routing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.sandbox import _opensandbox_internals as internals
from core.sandbox._opensandbox_session import _OpenSandboxSessionMixin

_FLAGS = (
    "opensandbox_myspace_bind_mount_enabled",
    "dws_creds_bind_mount_enabled",
    "lark_creds_bind_mount_enabled",
    "email_creds_bind_mount_enabled",
    "yida_creds_bind_mount_enabled",
)


def _settings(**overrides):
    values = {name: False for name in _FLAGS}
    # Host storage configured means the skill view is per-user too; off by
    # default here so each flag can be tested on its own.
    values["opensandbox_host_storage_path"] = ""
    values.update(overrides)
    return SimpleNamespace(sandbox=SimpleNamespace(**values))


@pytest.mark.parametrize("enabled_flag", _FLAGS)
def test_each_private_mount_requires_user_bound_sandbox(monkeypatch, enabled_flag):
    monkeypatch.setattr(internals, "settings", _settings(**{enabled_flag: True}))

    assert internals._user_bound_sandbox_required() is True


def test_per_user_skill_view_requires_user_bound_sandbox(monkeypatch):
    """Host storage alone is enough: /workspace/skills is that user's own view."""
    monkeypatch.setattr(internals, "settings", _settings(opensandbox_host_storage_path="/srv/storage"))

    assert internals._user_bound_sandbox_required() is True


def test_all_private_mounts_disabled_allow_general_pool(monkeypatch):
    monkeypatch.setattr(internals, "settings", _settings())

    assert internals._user_bound_sandbox_required() is False


def test_dingtalk_mount_uses_user_pool_when_myspace_mount_is_disabled(monkeypatch):
    """HugAgentOS regression: dws=true + myspace=false must still mount user credentials."""
    monkeypatch.setattr(
        internals,
        "settings",
        _settings(dws_creds_bind_mount_enabled=True),
    )
    provider = _OpenSandboxSessionMixin()
    user_sandbox = SimpleNamespace(id="user-sandbox")
    provider._jupyter_user_pool = SimpleNamespace(acquire=AsyncMock(return_value=user_sandbox))
    provider._pool = SimpleNamespace(acquire=AsyncMock())

    session = asyncio.run(provider._create_session(user_id="user-1"))

    assert session.sandbox is user_sandbox
    assert session.pool_source == "user"
    provider._jupyter_user_pool.acquire.assert_awaited_once_with("user-1")
    provider._pool.acquire.assert_not_awaited()


def test_missing_user_id_still_uses_general_pool(monkeypatch):
    monkeypatch.setattr(
        internals,
        "settings",
        _settings(dws_creds_bind_mount_enabled=True),
    )
    provider = _OpenSandboxSessionMixin()
    general_sandbox = SimpleNamespace(id="general-sandbox")
    provider._jupyter_user_pool = SimpleNamespace(acquire=AsyncMock())
    provider._pool = SimpleNamespace(acquire=AsyncMock(return_value=general_sandbox))

    session = asyncio.run(provider._create_session(user_id=None))

    assert session.sandbox is general_sandbox
    assert session.pool_source == "general"
    provider._pool.acquire.assert_awaited_once_with("jupyter")
    provider._jupyter_user_pool.acquire.assert_not_awaited()
