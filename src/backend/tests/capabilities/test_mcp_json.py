"""mcp.json：单写入者、generation 校验、损坏不重置、local 区校验、managed 投影、凭据引用。"""

from __future__ import annotations

import json

import pytest
from core.capabilities import credentials, mcp_json


@pytest.fixture(autouse=True)
def _mem_store(monkeypatch, caps_root):
    monkeypatch.setenv("HUGAGENT_CREDENTIAL_BACKEND", "memory")
    credentials._memory_store.clear()


def test_empty_then_write_and_generation_conflict(caps_root):
    doc = mcp_json.load()
    assert doc.generation == 0 and doc.local == {} and doc.managed == {}
    doc.local["files"] = mcp_json._validate_local_server(
        "files", {"transport": "stdio", "command": "python"}
    )
    written = mcp_json.write(doc, expected_generation=0)
    assert written.generation == 1 and (caps_root / "mcp.json").exists()
    stale = mcp_json.McpJson(generation=0)
    with pytest.raises(mcp_json.McpJsonConflict):
        mcp_json.write(stale, expected_generation=0)
    on_disk = json.loads((caps_root / "mcp.json").read_text())
    assert (
        on_disk["schemaVersion"] == 2
        and on_disk["local"]["servers"]["files"]["command"] == "python"
    )


def test_corrupt_file_is_kept_until_explicit_repair(caps_root):
    path = caps_root / "mcp.json"
    caps_root.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(mcp_json.McpJsonCorrupt):
        mcp_json.load()
    assert path.read_text() == "{not json"
    moved = mcp_json.quarantine_corrupt()
    assert moved is not None and moved.exists() and not path.exists()
    assert mcp_json.load().generation == 0


def test_local_server_validation_rejects_inline_headers_and_bad_transports():
    with pytest.raises(mcp_json.McpJsonError):
        mcp_json._validate_local_server(
            "x", {"transport": "sse", "url": "https://a", "headers": {"k": "v"}}
        )
    with pytest.raises(mcp_json.McpJsonError):
        mcp_json._validate_local_server("x", {"transport": "grpc"})
    with pytest.raises(mcp_json.McpJsonError):
        mcp_json._validate_local_server("x", {"transport": "streamable_http", "url": "ftp://a"})
    with pytest.raises(ValueError):
        mcp_json._validate_local_server("../x", {"transport": "stdio", "command": "a"})


def test_upsert_local_server_stores_headers_in_credential_store(caps_root):
    mcp_json.upsert_local_server(
        "my-api",
        {"transport": "streamable_http", "url": "https://api.example/mcp"},
        secret_headers={"Authorization": "Bearer s3cr3t"},
    )
    raw = (caps_root / "mcp.json").read_text()
    assert "s3cr3t" not in raw and "os-store:mcp-my-api" in raw
    cfgs = mcp_json.local_server_configs()
    assert cfgs["my-api"]["headers"] == {"Authorization": "Bearer s3cr3t"}
    assert cfgs["my-api"]["execution_scope"] == "local"
    assert mcp_json.remove_local_server("my-api") is True
    assert credentials.load_secret("os-store:mcp-my-api") is None
    assert mcp_json.load().local == {}


def test_managed_projection_preserves_local_and_enable_flags(caps_root):
    mcp_json.upsert_local_server(
        "files", {"transport": "stdio", "command": "python", "args": ["-m", "x"]}
    )
    servers = [
        {
            "server_id": "internet_search",
            "component": "internet_search",
            "schema_hash": "h1",
            "display_name": "搜索",
        },
        {"server_id": "kb", "component": "kb", "schema_hash": "h2"},
    ]
    mcp_json.project_managed_profile(
        "p_1", cloud_instance_id="cloud.example", catalog_revision="rev-1", servers=servers
    )
    mcp_json.set_managed_enabled("p_1", "kb", False)
    # re-projection with a new revision keeps the user's enable flag and the local scope
    mcp_json.project_managed_profile(
        "p_1", cloud_instance_id="cloud.example", catalog_revision="rev-2", servers=servers
    )
    doc = mcp_json.load()
    assert doc.local["files"]["args"] == ["-m", "x"]
    assert doc.managed["p_1"]["catalogRevision"] == "rev-2"
    assert mcp_json.managed_enabled("p_1") == {"internet_search": True, "kb": False}
    assert (
        doc.managed["p_1"]["servers"]["internet_search"]["resourceRef"]["issuer"] == "cloud.example"
    )
    assert "url" not in json.dumps(doc.managed)  # no upstream address ever lands here
