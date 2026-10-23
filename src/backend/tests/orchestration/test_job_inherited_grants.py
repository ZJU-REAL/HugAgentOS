"""Independent fork chats retain validated parent activation grants."""

import logging
from types import SimpleNamespace

import pytest
from core.llm.factory.models import RequiredCapabilities, StickyCapabilities
from core.llm.factory.request import AgentRequest
from core.llm.factory.selection.sticky_capabilities import sticky_capabilities


@pytest.mark.asyncio
async def test_fork_reads_parent_grants_without_gaining_later_activations(monkeypatch):
    from features.desktop.capabilities import paths
    from core.llm.runtime import session_capabilities
    from core.capabilities.plugins import runtime as plugins

    seen = []

    def direct(**kwargs):
        seen.append(kwargs["chat_id"])
        return SimpleNamespace(
            skill_ids=["explicit-skill", "later-skill"], mcp_ids=["disabled-private", "later-mcp"]
        )

    monkeypatch.setattr(session_capabilities, "resolve_session_activated_capabilities", direct)
    monkeypatch.setattr(
        plugins,
        "resolve_sticky_plugin_capabilities",
        lambda **_: SimpleNamespace(install_ids=[], unavailable_ids=[], skill_ids=[], mcp_ids=[]),
    )
    monkeypatch.setattr(paths, "capabilities_enabled", lambda: False)
    # Select the default cloud provider in the phase for deterministic inherited grants.
    import importlib

    phase = importlib.import_module("core.llm.factory.selection.sticky_capabilities")
    monkeypatch.setattr(phase, "has_run_capabilities", lambda: False)
    request = AgentRequest(
        derived_main=True,
        chat_id="fork-chat",
        capability_source_chat_id="parent-chat",
        current_user_id="user",
        enabled_skill_ids=["explicit-skill"],
        enabled_mcp_ids=["disabled-private"],
    )
    (sticky,) = await sticky_capabilities(
        request,
        _elapsed=lambda: "",
        _eval_scope=None,
        _log=logging.getLogger(__name__),
        _mode_manual_invoke=True,
        _note_unavailable=lambda _: None,
        required=RequiredCapabilities(),
        sticky=StickyCapabilities(),
    )
    assert seen == ["parent-chat"]
    assert sticky.direct_skill_ids == ["explicit-skill"]
    assert sticky.direct_mcp_ids == ["disabled-private"]
    assert request.enabled_skill_ids == ["explicit-skill"]
    assert request.enabled_mcp_ids == ["disabled-private"]
    assert request.chat_id == "fork-chat"

    # The grant reaches the real owned-server gate despite a personal disabled switch.
    from core.llm.factory.selection.server_selection import server_selection
    from core.llm.factory.tools import mcp_config
    from features.desktop.bridge.gateway import cloud_gateway_mcp_configs
    from core.capabilities.mcp.service import McpServerConfigService

    calls = []

    class Config:
        def get_owned_servers(self, user, enabled_only=True):
            calls.append((user, enabled_only))
            return {} if enabled_only else {"disabled-private": {"transport": "stdio"}}

    monkeypatch.setattr(McpServerConfigService, "get_instance", lambda: Config())
    monkeypatch.setattr(
        'features.desktop.bridge.gateway.cloud_gateway_mcp_configs', lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        mcp_config,
        'effective_mcp_server_keys',
        lambda *_a, **k: [m for m in k["enabled_mcp_ids"] if m in k["owned_servers"]],
    )
    result = await server_selection(
        request,
        _api_scope=None,
        _eval_scope=None,
        _elapsed=lambda: "",
        _log=logging.getLogger(__name__),
        _note_unavailable=lambda _: None,
        required=RequiredCapabilities(),
        sticky=sticky,
        cfg=None,
        skill_bound_mcp_ids=[],
    )
    assert calls == [("user", False)]
    assert result[3] == ["disabled-private"]
