"""A tool the platform knows only by name must never reach the model.

The desktop builds its model-facing tools from the capability manifest the cloud
ships, which is ``AdminMcpServer.tools_json`` verbatim. When a plugin manifest
overwrote that column with its display-only ``{name, description}`` list, every
tool arrived without parameters: the model had nowhere to put ``src_dir`` and
publishing a site retried the same blank call until the run was cancelled.
"""

from __future__ import annotations

import pytest

from agentscope.mcp import HttpMCPConfig

from core.llm.mcp_manager import ManifestMCPClient, has_usable_schema
from core.llm.mcp_pool import uses_manifest_schema
from core.services.plugin_service import _merge_tool_metadata

REAL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "src_dir": {"type": "string", "default": ""},
    },
    "required": ["title"],
}
ARGLESS_SCHEMA = {"type": "object", "properties": {}, "required": []}


def _cfg(tools):
    return {
        "schema_source": "cloud_manifest",
        "gateway_invoke_url": "https://cloud.example/api/v1/desktop/capability/gateway/s/call",
        "schema_hash": "hash",
        "manifest_tools": tools,
    }


def test_display_only_entry_is_not_a_usable_schema():
    assert not has_usable_schema({"name": "publish_site", "description": "..."})
    assert not has_usable_schema({"name": "publish_site", "inputSchema": {}})


def test_argument_less_tool_is_still_a_captured_schema():
    """A server may describe an argument-less tool tersely; that is not a gap."""
    assert has_usable_schema({"name": "list_project_sites", "inputSchema": ARGLESS_SCHEMA})
    assert has_usable_schema({"name": "list_project_sites", "inputSchema": {"type": "object"}})
    assert uses_manifest_schema(
        _cfg([{"name": "list_project_sites", "inputSchema": {"type": "object"}}])
    )


def test_manifest_without_any_schema_is_rejected():
    assert not uses_manifest_schema(_cfg([{"name": "publish_site", "description": "..."}]))


def test_client_withholds_the_tool_it_cannot_describe():
    client = ManifestMCPClient(
        name="sites-site_publish",
        is_stateful=False,
        mcp_config=HttpMCPConfig(url="https://cloud.example/call"),
        manifest_tools=[
            {"name": "publish_site", "description": "发布站点"},
            {"name": "list_project_sites", "description": "查询站点", "inputSchema": ARGLESS_SCHEMA},
        ],
        gateway_invoke_url="https://cloud.example/call",
        schema_hash="hash",
    )
    names = [tool.name for tool in client._raw_manifest_tools()]
    assert names == ["list_project_sites"], "参数丢失的工具不能交给模型"


def test_captured_schema_survives_a_plugin_upgrade():
    """Reinstalling or upgrading the plugin must not blank out probed schemas."""
    stored = [
        {"name": "publish_site", "description": "旧描述", "inputSchema": REAL_SCHEMA},
    ]
    manifest = [{"name": "publish_site", "description": "新描述"}]

    merged = _merge_tool_metadata(stored, manifest)

    assert len(merged) == 1
    assert merged[0]["description"] == "新描述", "清单描述应生效"
    assert merged[0]["inputSchema"] == REAL_SCHEMA, "探测到的参数 schema 不能被清单覆盖"
    assert has_usable_schema(merged[0])


def test_manifest_only_tool_is_still_recorded():
    merged = _merge_tool_metadata([], [{"name": "publish_site", "description": "发布站点"}])
    assert [item["name"] for item in merged] == ["publish_site"]
    assert not has_usable_schema(merged[0])


@pytest.mark.parametrize("stored", [None, "not-a-list", []])
def test_merge_tolerates_missing_stored_metadata(stored):
    merged = _merge_tool_metadata(stored, [{"name": "t", "description": "d"}])
    assert [item["name"] for item in merged] == ["t"]
