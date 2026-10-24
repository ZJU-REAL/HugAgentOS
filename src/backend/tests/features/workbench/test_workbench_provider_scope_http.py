"""Approved Workbench provider must not enter legacy global service clients."""
import asyncio
import io
import json
import types
import zipfile

from features.workbench.ui.data_proxy import call_data_source
from extensions.mcp_servers.ai_chain_information_mcp import ui_data
from tests.features.workbench.test_workbench_application_config_http import HOST, boundary, install, package


def provider_package():
    original = zipfile.ZipFile(io.BytesIO(package(name="provider-isolation")))
    manifest = json.loads(original.read("plugin.json"))
    manifest["extensions"]["org.hugagent"]["ui"]["contributes"]["data_sources"] = [{
        "id": "records",
        "provider": "extensions.mcp_servers.ai_chain_information_mcp.ui_data:node_companies",
    }]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        archive.writestr("web/index.html", "<h1>Provider</h1>")
    return output.getvalue()


def fake_provider_client(monkeypatch):
    calls = []

    class Client:
        def __init__(self):
            calls.append("constructor")

        async def request(self, *_args, **_kwargs):
            calls.append("request")
            return {"数据": {"dataList": [], "总数": 0}}

    def configuration():
        calls.append("configuration")
        return types.SimpleNamespace(base_url="https://controlled-provider.example/api")

    monkeypatch.setattr(ui_data, "IndustryKnowledgeClient", Client)
    monkeypatch.setattr(ui_data, "resolve_industry_config", configuration)
    return calls


def test_real_session_approved_provider_never_reads_global_config_or_calls_client(boundary, monkeypatch):
    clients, _, owned_http_calls = boundary
    calls = fake_provider_client(monkeypatch)
    _, path, body = install(clients["owner"], provider_package())
    response = clients["owner"].post(path + "/data/query", headers=HOST, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "unsupported_capability"
    assert calls == []
    assert owned_http_calls == []


def test_legacy_default_provider_still_runs_registered_function(monkeypatch):
    calls = fake_provider_client(monkeypatch)
    result = asyncio.run(call_data_source(
        {"id": "records",
         "provider": "extensions.mcp_servers.ai_chain_information_mcp.ui_data:node_companies"},
        {}, slug="industry-knowledge-center",
    ))
    assert result["items"] == []
    assert calls == ["constructor", "request", "configuration"]
