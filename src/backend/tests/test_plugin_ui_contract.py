"""Plugin UI contribution contract: validation, projection, proxy and hosting.

Covers the three properties the design leans on:

1. **Fail-soft validation** — a malformed contribution is dropped with a reason
   rather than making the plugin uninstallable.
2. **Credentials never reach the browser** — the public projection strips a data
   source's upstream URL and auth header.
3. **The proxy and the module host are the only doors** — parameters are bounded
   by the declaration, unconfigured credentials fail loudly, and asset paths
   cannot escape the plugin package.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.infra.exceptions import BadRequestError
from core.services import plugin_data_proxy as proxy
from core.services.plugin_ui_contract import (
    SUPPORTED_UI_VERSION,
    VIEW_KINDS,
    find_data_source,
    find_module,
    normalize_ui,
    public_contributions,
)

IKC_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "plugin_bundles/marketplace/industry-knowledge-center/plugin.json"
)


def _minimal_ui(**contributes):
    return {"version": 1, "contributes": contributes}


# ── Validation ───────────────────────────────────────────────────────────────

def test_absent_block_is_not_an_error():
    ui, dropped = normalize_ui(None)
    assert ui is None and dropped == []


def test_future_version_is_ignored_wholesale():
    ui, dropped = normalize_ui({"version": SUPPORTED_UI_VERSION + 1, "contributes": {"tool_meta": []}})
    assert ui is None
    assert any("契约版本" in d["reason"] for d in dropped)


def test_unknown_view_kind_is_dropped_with_a_reason():
    ui, dropped = normalize_ui(
        _minimal_ui(tool_views=[{"tool": "t", "view": "hologram", "map": {}}])
    )
    assert ui is None  # nothing else survived
    assert any("未知 view 类型" in d["reason"] for d in dropped)


def test_one_bad_entry_does_not_take_the_good_ones_down():
    ui, dropped = normalize_ui(
        _minimal_ui(
            tool_views=[
                {"tool": "good_tool", "view": "list", "map": {"items": "$.items[]"}},
                {"tool": "bad_tool", "view": "not-a-view"},
            ]
        )
    )
    assert ui is not None
    assert [e["tools"] for e in ui["contributes"]["tool_views"]] == [["good_tool"]]
    assert len(dropped) == 1


def test_every_declared_view_kind_is_accepted():
    ui, dropped = normalize_ui(
        _minimal_ui(tool_views=[{"tool": f"t{i}", "view": kind} for i, kind in enumerate(VIEW_KINDS)])
    )
    assert ui is not None
    assert len(ui["contributes"]["tool_views"]) == len(VIEW_KINDS)
    assert dropped == []


def test_pointer_grammar_is_a_whitelist():
    """Anything outside the documented pointer forms is discarded, not evaluated."""
    ui, _ = normalize_ui(
        _minimal_ui(
            tool_views=[{
                "tool": "t",
                "view": "list",
                "map": {
                    "items": "$.items[]",
                    "nested": "$.a.b.c",
                    "evil": "$.items[0]; fetch('//x')",
                    "alsoEvil": "${constructor}",
                },
            }]
        )
    )
    mapping = ui["contributes"]["tool_views"][0]["map"]
    assert mapping["items"] == "$.items[]"
    assert mapping["nested"] == "$.a.b.c"
    assert "evil" not in mapping
    assert "alsoEvil" not in mapping


def test_module_entry_must_stay_inside_the_package_web_dir():
    ui, dropped = normalize_ui(
        _minimal_ui(modules=[
            {"id": "escape", "entry": "web/../../../etc/passwd", "surface": "canvas"},
            {"id": "outside", "entry": "assets/index.html", "surface": "canvas"},
            {"id": "ok", "entry": "web/x/index.html", "surface": "canvas"},
        ])
    )
    assert [m["id"] for m in ui["contributes"]["modules"]] == ["ok"]
    assert len(dropped) == 2


def test_module_grants_are_filtered_to_known_capabilities():
    ui, _ = normalize_ui(
        _minimal_ui(modules=[{
            "id": "m", "entry": "web/m/index.html", "surface": "canvas",
            "grants": ["data_source:ds", "theme", "chat.send", "eval.anything", "fs.read"],
        }])
    )
    # theme/locale are not grants (the bridge delivers them unconditionally), so
    # they are filtered along with unknown capability names.
    assert ui["contributes"]["modules"][0]["grants"] == ["data_source:ds", "chat.send"]


def test_actions_are_a_list_available_to_any_view():
    ui, _ = normalize_ui(
        _minimal_ui(tool_views=[{
            "tool": "t", "view": "ranking",
            "actions": [
                {"id": "a", "data_source": "ds", "trigger": "item",
                 "result": {"view": "list", "paged": True, "page_param": "pageNum"}},
                {"id": "b", "data_source": "ds2", "trigger": "node"},
            ],
        }])
    )
    actions = ui["contributes"]["tool_views"][0]["actions"]
    assert [a["id"] for a in actions] == ["a", "b"]
    assert actions[0]["result"]["page_param"] == "pageNum"
    assert actions[0]["result"]["page_size_param"] == "page_size"  # default


def test_i18n_map_and_plain_string_labels_both_survive():
    ui, _ = normalize_ui(
        _minimal_ui(tool_meta=[
            {"tool": "a", "label": {"zh-CN": "中文", "en": "English"}},
            {"tool": "b", "label": "只有中文"},
        ])
    )
    metas = {m["tool"]: m["label"] for m in ui["contributes"]["tool_meta"]}
    assert metas["a"] == {"zh-CN": "中文", "en": "English"}
    assert metas["b"] == "只有中文"


# ── Public projection ────────────────────────────────────────────────────────

def test_public_projection_strips_credentials():
    ui, _ = normalize_ui(
        _minimal_ui(data_sources=[{
            "id": "ds",
            "url": "{config.industry.url}/x",
            "auth": {"header": "Authorization", "value": "Bearer {config.industry.auth_token}"},
            "params_schema": {"q": {"type": "string", "required": True}},
        }])
    )
    public = public_contributions(ui, slug="p")
    blob = json.dumps(public, ensure_ascii=False)

    assert "auth_token" not in blob
    assert "config.industry" not in blob
    assert "Authorization" not in blob
    source = public["contributes"]["data_sources"][0]
    assert source == {"id": "ds", "method": "POST", "params_schema": {"q": {"type": "string", "required": True}}}


# ── The shipped industry declaration ─────────────────────────────────────────

def test_industry_plugin_declaration_validates_cleanly():
    manifest = json.loads(IKC_MANIFEST.read_text(encoding="utf-8"))
    ui, dropped = normalize_ui(manifest["extensions"]["org.hugagent"]["ui"])

    assert dropped == [], f"declaration has rejected entries: {dropped}"
    contributes = ui["contributes"]
    assert contributes["tool_views"], "no tool views declared"
    assert find_data_source(ui, "node_companies") is not None
    assert find_module(ui, "chain-overview") is not None

    # The graph canvas must keep its node drill-down restricted to the tool whose
    # ids the upstream actually recognises.
    graph = contributes["canvas_views"][0]
    assert graph["options"]["default_levels"] == 3
    assert graph["options"]["max_levels"] == 16
    action = graph["actions"][0]
    assert action["enabled_for_tools"] == ["get_chain_information"]


def test_industry_plugin_does_not_add_a_homepage_shortcut():
    manifest = json.loads(IKC_MANIFEST.read_text(encoding="utf-8"))
    ui, _ = normalize_ui(manifest["extensions"]["org.hugagent"]["ui"])

    assert ui["contributes"].get("shortcuts", []) == []


def test_industry_declaration_only_references_tools_the_plugin_exposes():
    manifest = json.loads(IKC_MANIFEST.read_text(encoding="utf-8"))
    ext = manifest["extensions"]["org.hugagent"]
    declared_tools = {
        tool["name"]
        for server in ext["mcp"].values()
        for tool in (server.get("tools") or [])
    }
    ui, _ = normalize_ui(ext["ui"])
    contributes = ui["contributes"]

    referenced = {m["tool"] for m in contributes["tool_meta"]}
    for view in contributes["tool_views"]:
        referenced.update(view["tools"])
    for canvas in contributes["canvas_views"]:
        referenced.update(canvas.get("auto_open_on_tools") or [])
    for module in contributes["modules"]:
        referenced.update(module.get("for_tools") or [])

    unknown = referenced - declared_tools
    assert not unknown, f"UI declaration references tools this plugin does not expose: {sorted(unknown)}"


def test_industry_module_entry_exists_in_the_package():
    manifest = json.loads(IKC_MANIFEST.read_text(encoding="utf-8"))
    ui, _ = normalize_ui(manifest["extensions"]["org.hugagent"]["ui"])
    entry = find_module(ui, "chain-overview")["entry"]
    module_path = IKC_MANIFEST.parent / entry
    assert module_path.is_file(), f"missing module asset: {entry}"

    # The self-shipped overview must traverse the same depth as the host canvas;
    # otherwise deeper nodes appear in one view but disappear from the other.
    graph = ui["contributes"]["canvas_views"][0]
    max_levels = graph["options"]["max_levels"]
    assert f"var MAX_LEVELS = {max_levels};" in module_path.read_text(encoding="utf-8")


def test_industry_declared_icons_ship_with_the_package():
    """Icons live in the plugin package, not in the host frontend tree."""
    manifest = json.loads(IKC_MANIFEST.read_text(encoding="utf-8"))
    ui, _ = normalize_ui(manifest["extensions"]["org.hugagent"]["ui"])

    icons = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "icon" and isinstance(value, str):
                    icons.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(ui)
    assert icons, "declaration ships no icons"
    for icon in icons:
        assert (IKC_MANIFEST.parent / icon).is_file(), f"declared icon missing from package: {icon}"


# ── Parameter validation ─────────────────────────────────────────────────────

def test_params_are_clamped_and_unknown_keys_dropped():
    schema = {
        "chainId": {"type": "string", "required": True},
        "pageNum": {"type": "integer", "default": 1, "min": 1, "max": 100},
    }
    out = proxy.validate_params(schema, {"chainId": "c1", "pageNum": 9999, "smuggled": "x"})
    assert out == {"chainId": "c1", "pageNum": 100}


def test_missing_required_param_is_rejected():
    with pytest.raises(BadRequestError):
        proxy.validate_params({"chainId": {"type": "string", "required": True}}, {})


def test_defaults_are_applied_when_absent():
    out = proxy.validate_params({"pageSize": {"type": "integer", "default": 10}}, {})
    assert out == {"pageSize": 10}


# ── Proxy safety ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unconfigured_credentials_fail_loudly(monkeypatch):
    monkeypatch.setattr(proxy, "_config_value", lambda key: "")
    source = {"id": "ds", "url": "{config.industry.url}/x", "method": "POST", "params_schema": {}}
    with pytest.raises(proxy.PluginDataSourceError) as excinfo:
        await proxy.call_data_source(source, {}, slug="p")
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_public_http_target_outside_the_admin_allowlist_is_refused(monkeypatch):
    """A plugin cannot aim the proxy at an arbitrary host over plain HTTP."""
    monkeypatch.setattr(proxy, "_configured_hosts", lambda slug: set())
    source = {"id": "ds", "url": "http://example.com/x", "method": "POST", "params_schema": {}}
    with pytest.raises(proxy.PluginDataSourceError) as excinfo:
        await proxy.call_data_source(source, {}, slug="p")
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_metadata_address_is_refused(monkeypatch):
    monkeypatch.setattr(proxy, "_configured_hosts", lambda slug: set())
    source = {"id": "ds", "url": "https://169.254.169.254/latest/meta-data/", "method": "GET", "params_schema": {}}
    with pytest.raises(proxy.PluginDataSourceError):
        await proxy.call_data_source(source, {}, slug="p")


@pytest.mark.asyncio
async def test_admin_configured_host_may_be_internal(monkeypatch):
    """On-prem deployments legitimately sit on private addresses."""
    monkeypatch.setattr(proxy, "_configured_hosts", lambda slug: {"10.0.0.5"})
    # Should pass the target check; the request itself is not made here.
    await proxy._check_target("http://10.0.0.5/api/x", "p")


# ── Module hosting ───────────────────────────────────────────────────────────

def test_module_asset_path_resolves_inside_the_package():
    target = proxy.module_asset_path("industry-knowledge-center", "web/chain-overview/index.html")
    assert target is not None and target.is_file()
    # The declaration's `web/` prefix is optional at request time.
    assert proxy.module_asset_path("industry-knowledge-center", "chain-overview/index.html") == target


def test_module_asset_path_refuses_traversal():
    for attempt in ("../../plugin.json", "web/../../plugin.json", "/etc/passwd", "../mcp.json"):
        assert proxy.module_asset_path("industry-knowledge-center", attempt) is None, attempt


def test_module_asset_path_is_none_for_unknown_plugin():
    assert proxy.module_asset_path("no-such-plugin", "web/index.html") is None


# ── Cross-language contract drift ────────────────────────────────────────────

def test_backend_view_kinds_match_the_frontend_registry():
    """The two ends of the contract must name the same view kinds.

    A view registered in the frontend but missing from ``VIEW_KINDS`` is dropped
    at install time and silently never renders; one listed here but absent from
    the registry renders as "unsupported view type" at runtime. Neither shows up
    in a type check, so it is pinned here.
    """
    # tests/ -> backend/ -> src/
    registry = Path(__file__).resolve().parents[2] / "frontend/src/plugin-ui/registry.ts"
    if not registry.is_file():  # CE tree ships the library; a bare backend checkout may not
        pytest.skip("frontend registry not present in this tree")

    source = registry.read_text(encoding="utf-8")
    body = source.split("VIEW_REGISTRY: Record<ViewKind, ViewEntry> = {", 1)[1]
    body = body.split("\n};", 1)[0]

    # Entries look like `badge: { … }` or `'tree-graph': { … }`.
    frontend_kinds = set(re.findall(r"^\s*'?([a-z-]+)'?\s*:\s*\{", body, re.M))

    assert frontend_kinds == set(VIEW_KINDS), (
        f"view kinds drifted — only in frontend: {sorted(frontend_kinds - set(VIEW_KINDS))}, "
        f"only in backend: {sorted(set(VIEW_KINDS) - frontend_kinds)}"
    )
