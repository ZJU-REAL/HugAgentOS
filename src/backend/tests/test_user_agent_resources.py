"""Bindable resource coverage for user-created sub-agents."""

from types import SimpleNamespace

import pytest

from core.db.models import AdminMcpServer
from core.services.user_agent_service import UserAgentService


def _mcp(
    server_id: str,
    *,
    owner_user_id: str | None = None,
    enabled: bool = True,
    source_plugin: str | None = None,
) -> AdminMcpServer:
    return AdminMcpServer(
        server_id=server_id,
        display_name=server_id.replace("_", " ").title(),
        description=f"{server_id} description",
        transport="streamable_http",
        url=f"https://example.test/{server_id}/mcp/",
        owner_user_id=owner_user_id,
        source_plugin=source_plugin,
        is_enabled=enabled,
    )


def test_available_resources_include_personally_disabled_mcps(
    db_session,
    monkeypatch,
):
    db_session.add_all(
        [
            _mcp("global_enabled"),
            _mcp("global_admin_disabled", enabled=False),
            _mcp("private_disabled", owner_user_id="user-a", enabled=False),
            _mcp("foreign_private", owner_user_id="user-b", enabled=False),
            _mcp("plugin_tool", owner_user_id="user-a", source_plugin="sample-plugin"),
        ]
    )
    db_session.commit()

    monkeypatch.setattr(
        "core.config.catalog_resolver.resolve_all_runtime_enabled",
        lambda _db, _user_id: ([], [], ["global_enabled"]),
    )
    monkeypatch.setattr(
        "core.services.plugin_service.builtin_plugin_component_ids",
        lambda: (set(), {"builtin_plugin_tool"}),
    )
    monkeypatch.setattr(
        "core.config.catalog_runtime.get_runtime_catalog",
        lambda _db, include_runtime_details=False: {
            "mcp": [
                {
                    "id": "catalog_only",
                    "name": "Catalog only",
                    "description": "Catalog-defined umbrella tool",
                    "enabled": True,
                },
                {
                    "id": "catalog_admin_disabled",
                    "name": "Catalog disabled",
                    "description": "Administrator-disabled tool",
                    "enabled": False,
                },
                {
                    "id": "builtin_plugin_tool",
                    "name": "Plugin component",
                    "description": "Must be selected through its plugin",
                    "enabled": True,
                },
            ]
        },
    )

    resources = UserAgentService(db_session).list_available_resources(owner_user_id="user-a")
    mcps = {item["id"]: item for item in resources["mcp_servers"]}

    assert set(mcps) == {"global_enabled", "private_disabled", "catalog_only"}
    assert mcps["global_enabled"]["enabled"] is True
    assert mcps["private_disabled"]["enabled"] is False
    assert mcps["catalog_only"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_agent", "expected_enabled_only"),
    [
        (None, True),
        (
            SimpleNamespace(
                mcp_server_ids=["private_disabled"],
                skill_ids=[],
                kb_ids=[],
                plugin_ids=[],
            ),
            False,
        ),
    ],
)
async def test_agent_factory_loads_disabled_private_mcp_only_for_explicit_subagent(
    monkeypatch,
    user_agent,
    expected_enabled_only,
):
    from core.llm import agent_factory

    calls = []

    class FakeMcpService:
        def get_owned_servers(self, user_id, enabled_only=True):
            calls.append((user_id, enabled_only))
            return {}

    class StopAfterOwnedMcpResolution(Exception):
        pass

    monkeypatch.setattr(
        agent_factory.McpServerConfigService,
        "get_instance",
        classmethod(lambda _cls: FakeMcpService()),
    )
    monkeypatch.setattr(agent_factory, "load_prompt_config", lambda: object())
    monkeypatch.setattr(
        agent_factory,
        "_effective_mcp_server_keys",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StopAfterOwnedMcpResolution()),
    )

    with pytest.raises(StopAfterOwnedMcpResolution):
        await agent_factory.create_agent_executor(
            current_user_id="user-a",
            user_agent=user_agent,
        )

    assert calls == [("user-a", expected_enabled_only)]
