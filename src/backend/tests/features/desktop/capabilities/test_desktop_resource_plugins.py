"""Synced interactive plugins execute against device-owned conversations."""

import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException

ROOT = Path(__file__).parents[4] / "extensions/plugins/marketplace/browser-automation"


def test_export_includes_exact_worker_and_canvas(monkeypatch):
    from features.desktop.bridge import resource_plugins as resources

    monkeypatch.setattr(resources, "package_root", lambda installed: ROOT)
    files, native = resources.package_files(
        {
            "ui_contributions": {"contributes": {"modules": [{"resource": {"entry": "runtime"}}]}},
            "import_report": {
                "imported": [{"type": "mcp", "name": "browser_runtime", "id": "account-browser"}]
            },
        }
    )
    assert native == {"account-browser": "browser_runtime"}
    assert (
        files["package/runtime/browser_worker/server.py"]
        == (ROOT / "runtime/browser_worker/server.py").read_text()
    )
    assert "package/web/browser/index.html" in files
    assert all(not name.startswith("package/skills/") for name in files)


@pytest.fixture
def synced(monkeypatch, tmp_path):
    from features.desktop.capabilities import device_catalog, store, dependency
    from features.desktop.bridge import resource_plugins as resources

    root = tmp_path / "revision"
    (root / "package/runtime").mkdir(parents=True)
    definition = {
        "native_servers": {"account-browser": "browser_runtime"},
        "ui_contributions": {"contributes": {"modules": [{"resource": {"entry": "runtime"}}]}},
    }
    (root / "plugin.json").write_text(json.dumps(definition))
    inst = SimpleNamespace(
        key="browser-automation",
        install_id="plugin:account:browser-automation",
        profile_id="account",
        ready=True,
        enabled=True,
        resolved_revision="rev",
        content_hash="verified",
        payload={"components": {"mcp": ["account-browser"]}},
    )
    monkeypatch.setattr(
        device_catalog,
        "installations",
        lambda kind, user_id: [inst] if user_id == "device-user" else [],
    )
    monkeypatch.setattr(store, "get", lambda *args: SimpleNamespace(path=root))
    monkeypatch.setattr(dependency, "component_hash", lambda comp: "verified")
    import importlib

    config = importlib.import_module("core.config.settings")
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            deploy=SimpleNamespace(is_local=True), server=SimpleNamespace(mcp_host="127.0.0.1")
        ),
    )
    return resources, inst, root


def test_binding_uses_local_endpoint_and_exact_installation(synced):
    resources, inst, root = synced
    cfg = resources.local_config(
        {"server_id": "account-browser", "tools": []}, "device-user", "account"
    )
    assert cfg["owner_user_id"] == "device-user"
    assert cfg["plugin_install_id"] == inst.install_id
    assert cfg["execution_scope"] == "local"
    assert "127.0.0.1" in cfg["url"]
    assert "gateway_invoke_url" not in cfg
    assert resources.local_config({"server_id": "unrelated"}, "device-user", "account") is None
    assert resources.local_config({"server_id": "account-browser"}, "other-user", "account") is None
    assert (
        resources.local_config({"server_id": "account-browser"}, "device-user", "other-account")
        is None
    )


@pytest.mark.parametrize("failure", ["missing", "hash", "disabled"])
def test_broken_local_resource_never_falls_back_to_cloud(synced, monkeypatch, failure):
    from features.desktop.capabilities import dependency

    resources, inst, root = synced
    if failure == "missing":
        (root / "package/runtime").rmdir()
    if failure == "hash":
        monkeypatch.setattr(dependency, "component_hash", lambda comp: "tampered")
    if failure == "disabled":
        inst.enabled = False
    with pytest.raises(HTTPException):
        resources.local_config({"server_id": "account-browser"}, "device-user", "account")


def test_runtime_proof_preserves_device_user_chat_and_cloud_installation(synced, monkeypatch):
    from core.llm.factory.tools.mcp_config import inject_runtime_headers as _inject_runtime_headers
    from core.llm.mcp.invocation import verify
    from features.knowledge import edition as external_provider

    resources, inst, _ = synced
    monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "isolated-resource-proof")
    monkeypatch.setattr(external_provider, "runtime_request_context", lambda ids: ({}, {}))
    cfg = resources.local_config({"server_id": "account-browser"}, "device-user", "account")
    headers = _inject_runtime_headers(
        {"account-browser": cfg}, current_user_id="device-user", chat_id="device-chat"
    )["account-browser"]["headers"]
    # Real HTTP headers are case insensitive, unlike an ordinary dict.
    import httpx

    claims = verify(httpx.Headers(headers), "browser_runtime")
    assert (claims["user"], claims["chat"], claims["plugin"]) == (
        "device-user",
        "device-chat",
        inst.install_id,
    )


def test_uploaded_definition_gets_resource_assets_too(monkeypatch):
    from features.desktop.bridge import resource_plugins as resources
    from features.desktop.capability.entities import _plugin_files

    monkeypatch.setattr(resources, "package_root", lambda installed: ROOT)
    installed = {
        "install_id": "browser@user",
        "slug": "browser-automation",
        "name": "Browser",
        "ui_contributions": {"contributes": {"modules": [{"resource": {"entry": "runtime"}}]}},
        "import_report": {
            "imported": [{"type": "mcp", "name": "browser_runtime", "id": "account-browser"}]
        },
        "_uploaded_files": {"plugin.json": json.dumps({"name": "Customized browser"})},
    }
    files = _plugin_files(installed)
    assert json.loads(files["plugin.json"])["native_servers"] == {
        "account-browser": "browser_runtime"
    }
    assert json.loads(files["plugin.json"])["name"] == "Customized browser"
    assert "package/runtime/browser_worker/server.py" in files


def test_unrelated_uploaded_plugin_is_byte_for_byte_unchanged():
    from features.desktop.capability.entities import _plugin_files

    files = {"plugin.json": '{"name":"Untouched"}\n', "notes.txt": "owned content"}
    assert (
        _plugin_files(
            {
                "install_id": "unrelated@user",
                "slug": "unrelated",
                "name": "Untouched",
                "_uploaded_files": files,
            }
        )
        == files
    )


def test_native_client_keeps_the_cloud_authorized_tool_scope(synced):

    resources, _, _ = synced
    cfg = resources.local_config(
        {"server_id": "account-browser", "tools": [{"name": "browser_open"}]},
        "device-user",
        "account",
    )
    assert cfg["enable_tools"] == ["browser_open"]
