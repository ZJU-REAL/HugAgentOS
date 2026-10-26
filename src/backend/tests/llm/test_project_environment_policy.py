from types import SimpleNamespace

import pytest
from core.llm.factory.request import AgentRequest
from core.llm.runtime.project_environment import ProjectEnvironmentPolicy
from fastapi import HTTPException


def policy():
    return ProjectEnvironmentPolicy(
        agent_ids=frozenset({"analyst"}),
        plugin_ids=frozenset({"charts"}),
        skill_ids=frozenset({"plot"}),
        mcp_ids=frozenset({"data"}),
    )


def test_history_and_explicit_lists_cannot_widen_project_registration():
    request = AgentRequest(
        enabled_skill_ids=["plot", "other"],
        enabled_mcp_ids=["data", "foreign"],
        visible_subagents=[{"agent_id": "analyst"}, {"agent_id": "foreign-agent"}],
    )
    policy().restrict(request)
    assert request.enabled_skill_ids == ["plot"]
    assert request.enabled_mcp_ids == ["data"]
    assert request.visible_subagents == [{"agent_id": "analyst"}]


def test_explicit_outside_connector_is_rejected_before_assembly():
    with pytest.raises(HTTPException) as error:
        policy().check_invocation(AgentRequest(required_mcp_ids=["foreign"]))
    assert error.value.status_code == 403


def test_outside_agent_cannot_execute_even_if_passed_directly():
    with pytest.raises(HTTPException):
        policy().check_invocation(
            AgentRequest(user_agent=SimpleNamespace(agent_id="foreign-agent"))
        )


def test_dynamic_plugin_loader_refuses_components_outside_frozen_environment():
    import asyncio

    from core.llm.tools.plugin_activation import register_load_plugin
    from core.capabilities.plugins.runtime.models import DeferredPlugin

    class Collector:
        def append(self, tool):
            fn = tool._func
            _ = {"func_description": tool.description, "permission": getattr(tool, "dispatch_spec", None)}
            self.invoke = fn

    collector = Collector()
    target = DeferredPlugin(
        install_id="charts", slug="charts", name="Charts", description="", mcp_ids=["foreign"]
    )
    register_load_plugin(collector, {"charts": target}, {"project_environment": policy()})
    response = asyncio.run(collector.invoke("charts"))
    assert "未绑定" in str(response.content)


def test_disallowed_plugin_sharing_a_loose_skill_does_not_hide_that_skill():
    from core.capabilities.plugins.runtime.models import DeferredPlugin, ProgressiveResolution

    deferred = DeferredPlugin(
        install_id="outside", slug="outside", name="Outside", description="", skill_ids=["plot"]
    )
    resolution = ProgressiveResolution(
        directory=[deferred], deferred=[deferred], deferred_skill_ids={"plot"}
    )
    request = AgentRequest(enabled_skill_ids=[], enabled_mcp_ids=[])
    assert policy().restrict_plugins(resolution, request) is None
    assert request.enabled_skill_ids == ["plot"]


def test_custom_cloud_child_inherits_environment_without_file_context():
    from core.projects.local_workspace import child_project_context

    context = {
        "project_id": "cloud-project",
        "project_environment_config": {"mode": "custom", "mcp_ids": ["data"]},
        "project_environment_effective": {"mcp_ids": ["data"]},
        "project_instructions": "private prompt",
        "project_folder_id": "folder",
    }
    inherited = child_project_context({"project_ctx": context})
    assert inherited == {
        "project_id": "cloud-project",
        "project_environment_config": context["project_environment_config"],
        "project_environment_effective": context["project_environment_effective"],
    }


@pytest.mark.parametrize("project_available", [False, True])
def test_revocation_still_denies_and_logs_only_removed_ids(monkeypatch, caplog, project_available):
    from dataclasses import replace
    from core.llm.runtime import project_environment

    original = policy()
    current = replace(original, mcp_ids=frozenset()) if project_available else None
    monkeypatch.setattr(project_environment, "_resolve", lambda request: current)
    request = AgentRequest(run_id="run-test", project_environment=original)
    with pytest.raises(HTTPException) as error:
        project_environment.validate_project_environment(request)
    assert error.value.status_code == 403
    assert "project_environment_revoked run_id=run-test" in caplog.text
    assert "'mcp_ids': ['data']" in caplog.text
    assert f"project_available={project_available}" in caplog.text
