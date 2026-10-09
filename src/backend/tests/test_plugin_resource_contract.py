from core.plugins.ui.contract import normalize_ui, public_contributions

def test_resource_module_retains_safe_runtime_and_binding():
    ui, dropped = normalize_ui({"version": 2, "contributes": {"modules": [{
        "id": "browser", "entry": "web/browser/index.html", "surface": "canvas",
        "grants": ["resource.attach", "resource.command"],
        "resource": {"entry": "runtime", "callable": "browser_worker.server:main"},
        "resource_binding": "$.resource",
    }]}})
    assert not dropped
    module = ui["contributes"]["modules"][0]
    assert module["resource"]["callable"] == "browser_worker.server:main"
    public = public_contributions(ui, slug="fixture")
    assert public["contributes"]["modules"][0]["resource_binding"] == "$.resource"

def test_invalid_resource_path_cannot_escape_package():
    ui, dropped = normalize_ui({"version": 2, "contributes": {"modules": [{
        "id": "browser", "entry": "web/browser/index.html",
        "resource": {"entry": "../outside", "callable": "browser_worker.server:main"}
    }]}})
    assert ui is None or not ui["contributes"].get("modules")
    assert dropped

def test_browser_manifest_has_canvas_reopen_action():
    import json
    from pathlib import Path
    manifest = json.loads((Path(__file__).parents[1] / "plugin_bundles/marketplace/browser-automation/plugin.json").read_text())
    ui, dropped = normalize_ui(manifest["extensions"]["org.hugagent"]["ui"])
    assert not dropped
    view = ui["contributes"]["tool_views"][0]
    assert view["tools"] == ["browser_open"]
    assert view["primary_action"]["open_canvas"] == "browser"
