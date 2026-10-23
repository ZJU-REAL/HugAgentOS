"""Behavioral gates and native assembly order for contributed tools."""
from features.datasources import metadata
from unittest.mock import Mock

import pytest
from core.harness.registry import NativeToolContext, install_tools
from core.harness.registry import registry
from core.llm.factory.request import AgentRequest
from features import register_product_tools
from features.sites.tools import design_picker as design_picker_tool
from integrations.channels.tools import attachment as channel_attachment_tool
from features.datasources.tools import data_context as data_context_tool


@pytest.fixture
def contributions(monkeypatch):
    monkeypatch.setattr(registry.registration_state(), 'tools', {})
    register_product_tools()
    register_product_tools()  # startup registration is idempotent


@pytest.mark.asyncio
@pytest.mark.parametrize("skills,channel,automation,interactive,expected", [
    ([], False, False, True, []),
    (["site-builder"], False, False, True, [("design", True)]),
    (["site-builder"], True, False, True, [("design", False), ("attachment", None)]),
    (["site-builder"], False, True, True, [("design", False)]),
    (["site-builder"], False, False, False, [("design", False)]),
    ([], True, False, True, [("attachment", None)]),
])
async def test_product_gates(contributions, monkeypatch, skills, channel, automation, interactive, expected):
    calls = []
    monkeypatch.setattr(design_picker_tool, "register_choose_design", lambda toolkit, **kw: calls.append(("design", kw["interactive"])))
    monkeypatch.setattr(channel_attachment_tool, "register_channel_attachment", lambda toolkit, **kw: calls.append(("attachment", None)))
    context = NativeToolContext(AgentRequest(automation_run=automation), interactive, channel, None, None, skills, [], Mock())
    for stage in ("after_sandbox", "after_image", "after_workflow"):
        await install_tools(stage, object(), context)
    assert calls == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("servers,eligible,fail,expected", [
    (["query_database"], ["db"], False, []),
    (["db_query"], [], False, []),
    (["db_query"], ["db"], False, [["db"]]),
    (["es_query"], ["es"], False, [["es"]]),
    (["db_query"], [], True, []),
])
async def test_data_dictionary_gates(contributions, monkeypatch, servers, eligible, fail, expected):

    lookup = Mock(side_effect=RuntimeError("unavailable")) if fail else Mock(return_value=eligible)
    monkeypatch.setattr(metadata, 'eligible_datasource_ids', lookup)
    calls = []
    monkeypatch.setattr(data_context_tool, "register_get_data_context", lambda toolkit, ids: calls.append(ids))
    context = NativeToolContext(AgentRequest(), True, False, None, None, [], servers, Mock())
    await install_tools("after_workflow", object(), context)
    assert calls == expected
    assert lookup.call_count == (0 if servers == ["query_database"] else 1)
    assert context.logger.warning.call_count == int(fail)


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled,turbo,expected", [(False, False, ["after_sandbox", "after_image", "after_workflow"]), (True, False, []), (False, True, [])])
async def test_native_assembly_gates_and_order(monkeypatch, disabled, turbo, expected):
    import importlib
    native = importlib.import_module("core.llm.factory.tools.native")
    for name in vars(native):
        if name.startswith("register_"):
            monkeypatch.setattr(native, name, Mock())
    monkeypatch.setattr(native, "code_capability_enabled", lambda: False)
    import core.models.vision
    monkeypatch.setattr(core.models.vision, "resolve_vision_mode", lambda value: "none")
    stages = []
    async def install(stage, toolkit, context):
        stages.append(stage)
    monkeypatch.setattr(native, "install_tools", install)
    await native.native_tools(AgentRequest(disable_tools=disabled, turbo_mode=turbo),
        _api_scope=None, _eval_scope=None, _interactive=True, _is_channel_run=False,
        _log=Mock(), _proj_scope=None, _sbx_sess=None, _shared_subagent_cfg=None,
        _turbo_code_exec=False, allowed_skill_dirs=[], enabled_mcp_keys=[], loaded_skill_ids=[],
        loader=Mock(), skill_ids_to_register=[], toolkit=Mock())
    assert stages == expected


def test_headless_host_composes_tools_without_asgi():
    import subprocess
    import sys
    script = 'import asyncio\nimport logging\nimport sys\nfrom unittest.mock import patch\nfrom features import register_product_tools\nfrom core.harness.registry import NativeToolContext, install_tools\nfrom core.llm.factory.request import AgentRequest\nfrom core.llm.factory.tools.definitions import ToolAssembly\n\nassert "api.app" not in sys.modules\nregister_product_tools()\nregister_product_tools()\n\nasync def main():\n    toolkit = ToolAssembly()\n    context = NativeToolContext(AgentRequest(chat_id="test", current_user_id="test"), True, True, None, None, ["site-builder"], ["db_query"], logging.getLogger("test"))\n    with patch("features.datasources.metadata.eligible_datasource_ids", return_value=["fixture"]):\n        for stage in ("after_sandbox", "after_image", "after_workflow"):\n            await install_tools(stage, toolkit, context)\n    names = [tool.name for tool in list(toolkit)]\n    assert names == ["choose_design", "channel_read_attachment", "get_data_context"], names\n    assert "api.app" not in sys.modules\n    print("headless tool composition passed", names)\n\nasyncio.run(main())\n'
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
