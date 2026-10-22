"""The plugin installation boundary must validate applications without breaking old UI."""

from features.workbench.ui.contract import normalize_ui, public_contributions


def test_workspace_application_survives_installation_and_public_projection():
    raw = {
        "version": 3,
        "contributes": {
            "modules": [{"id": "main", "entry": "web/main.html", "surface": "workspace"}],
            "applications": [
                {
                    "schemaVersion": "1.0.0",
                    "id": "reports",
                    "title": "Reports",
                    "target": {"kind": "plugin", "moduleId": "main"},
                }
            ],
        },
    }
    normalized, dropped = normalize_ui(raw)
    assert dropped == []
    assert normalized["contributes"]["applications"][0]["id"] == "reports"
    assert (
        public_contributions(normalized, slug="reports")["contributes"]["modules"][0]["surface"]
        == "workspace"
    )


import json
import subprocess
from pathlib import Path

import pytest
from features.workbench.protocol import validate_application, validate_contributions

_ROOT = Path(__file__).resolve().parents[5]
_PACKAGE = _ROOT / "packages/workbench-protocol"
_APP_CASES = json.loads((_PACKAGE / "fixtures/applications.json").read_text())
_CONTRIBUTION_CASES = json.loads((_PACKAGE / "fixtures/contributions.json").read_text())


@pytest.mark.parametrize("case", _APP_CASES, ids=lambda c: c["name"])
def test_shared_application_fixture(case):
    errors = validate_application(case["value"], case["context"])
    assert (len(errors) == 0) is case["valid"]
    for path in case["paths"]:
        assert any(e["path"].startswith(path) for e in errors)
    assert "SECRET" not in json.dumps(errors)


@pytest.mark.parametrize("case", _CONTRIBUTION_CASES, ids=lambda c: c["name"])
def test_shared_contribution_fixture(case):
    result = validate_contributions(case["value"], {"source": "plugin"})
    assert [a["id"] for a in result["applications"]] == case["accepted"]
    assert [m["id"] for m in result["modules"]] == case["modules"]
    for path in case["paths"]:
        assert any(e["path"].startswith(path) for e in result["errors"])


def test_javascript_and_python_give_the_same_fixture_verdicts():
    raw = subprocess.check_output(["node", str(_PACKAGE / "test/verdicts.mjs")], text=True)
    javascript = json.loads(raw)
    assert javascript["applications"] == [
        not validate_application(c["value"], c["context"]) for c in _APP_CASES
    ]
    assert javascript["contributions"] == [
        {
            kind: [e["id"] for e in validate_contributions(c["value"], {"source": "plugin"})[kind]]
            for kind in ("applications", "modules")
        }
        for c in _CONTRIBUTION_CASES
    ]


def test_backend_schema_artifacts_equal_the_independent_package():
    for name in ("application.schema.json", "module.schema.json", "host.json"):
        assert (_PACKAGE / "schemas" / name).read_bytes() == (
            _ROOT / "src/backend/features/workbench/schemas" / name
        ).read_bytes()


def test_rejected_module_cannot_leave_a_clickable_application():
    raw = {
        "version": 3,
        "contributes": {
            "modules": [
                {
                    "id": "main",
                    "entry": "web/main.html",
                    "surface": "workspace",
                    "resource": {"entry": "../SECRET", "callable": "not-callable"},
                }
            ],
            "applications": [
                {
                    "schemaVersion": "1.0.0",
                    "id": "reports",
                    "title": "Reports",
                    "target": {"kind": "plugin", "moduleId": "main"},
                }
            ],
            "tool_views": [{"tool": "healthy_tool", "view": "list"}],
        },
    }
    normalized, dropped = normalize_ui(raw)
    assert not normalized["contributes"].get("applications")
    assert normalized["contributes"]["tool_views"][0]["tools"] == ["healthy_tool"]
    assert "SECRET" not in json.dumps(dropped)


def test_malformed_v3_legacy_view_does_not_break_healthy_application():
    raw = {
        "version": 3,
        "contributes": {
            "modules": [{"id": "main", "entry": "web/main.html", "surface": "workspace"}],
            "applications": [
                {
                    "schemaVersion": "1.0.0",
                    "id": "reports",
                    "title": "Reports",
                    "target": {"kind": "plugin", "moduleId": "main"},
                }
            ],
            "tool_views": [
                {
                    "tool": "bad",
                    "view": "list",
                    "actions": [{"id": "a", "data_source": "ds", "params": ["SECRET"]}],
                },
                {"tool": "healthy", "view": "list"},
            ],
        },
    }
    normalized, dropped = normalize_ui(raw)
    assert normalized["contributes"]["applications"][0]["id"] == "reports"
    assert normalized["contributes"]["tool_views"][0]["tools"] == ["healthy"]
    assert any(d["name"] == "/contributes/tool_views" for d in dropped)
    assert "SECRET" not in json.dumps(dropped)


def test_every_official_capability_is_valid_without_weakening_availability():
    host = json.loads((_PACKAGE / "schemas/host.json").read_text())["capabilities"]
    schema = json.loads((_PACKAGE / "schemas/application.schema.json").read_text())
    assert schema["definitions"]["capabilities"]["items"]["enum"] == host
    application = {
        "schemaVersion": "1.0.0", "id": "reports", "title": "Reports",
        "target": {"kind": "builtin", "componentId": "reports-page"},
    }
    for capability in host:
        value = {**application, "compatibility": {"requiredCapabilities": [capability]}}
        assert not validate_application(value, {"componentIds": ["reports-page"]})
    unavailable = {**application, "compatibility": {"requiredCapabilities": ["lifecycle.setDirty"]}}
    assert any(issue["code"] == "unsupported_capability" for issue in
               validate_application(unavailable, {"componentIds": ["reports-page"], "capabilities": []}))
