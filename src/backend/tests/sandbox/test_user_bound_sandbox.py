"""Regression tests for OpenSandbox per-user volume routing."""

from __future__ import annotations

from tests.sandbox.provider_test_support import patch_open_settings

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider

_FLAGS = (
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


def test_dingtalk_mount_uses_user_pool_when_myspace_mount_is_disabled(monkeypatch):
    """HugAgentOS regression: dws=true + myspace=false must still mount user credentials."""
    patch_open_settings(monkeypatch, _settings(dws_creds_bind_mount_enabled=True))
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    user_sandbox = SimpleNamespace(id="user-sandbox")
    provider._jupyter_user_pool = SimpleNamespace(acquire=AsyncMock(return_value=user_sandbox))
    provider._pool = SimpleNamespace(acquire=AsyncMock())

    session = asyncio.run(provider._create_session(user_id="user-1"))

    assert session.sandbox is user_sandbox
    assert session.pool_source == "user"
    provider._jupyter_user_pool.acquire.assert_awaited_once_with("user-1")
    provider._pool.acquire.assert_not_awaited()


def test_missing_user_id_still_uses_general_pool(monkeypatch):
    patch_open_settings(monkeypatch, _settings(dws_creds_bind_mount_enabled=True))
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    general_sandbox = SimpleNamespace(id="general-sandbox")
    provider._jupyter_user_pool = SimpleNamespace(acquire=AsyncMock())
    provider._pool = SimpleNamespace(acquire=AsyncMock(return_value=general_sandbox))

    session = asyncio.run(provider._create_session(user_id=None))

    assert session.sandbox is general_sandbox
    assert session.pool_source == "general"
    provider._pool.acquire.assert_awaited_once_with("jupyter")
    provider._jupyter_user_pool.acquire.assert_not_awaited()
