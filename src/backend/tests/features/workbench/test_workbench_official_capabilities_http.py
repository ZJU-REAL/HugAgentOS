"""Real session-authenticated package import for exact official capabilities."""
import io
import json
import zipfile

import pytest

from tests.features.workbench.test_workbench_revocation_failure_http import boundary


def capability_package(capability):
    from tests.features.workbench.workbench_plugin_support import package

    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package())) as original:
        with zipfile.ZipFile(output, "w") as archive:
            for name in original.namelist():
                value = original.read(name)
                if name == "plugin.json":
                    manifest = json.loads(value)
                    contributes = manifest["extensions"]["org.hugagent"]["ui"]["contributes"]
                    contributes["applications"][0]["compatibility"] = {
                        "requiredCapabilities": [capability],
                        "optionalCapabilities": [capability],
                    }
                    if capability != "unknown.feature":
                        contributes["modules"][0]["grants"] = [capability]
                    value = json.dumps(manifest)
                archive.writestr(name, value)
    return output.getvalue()


@pytest.mark.parametrize(
    "capability", ["lifecycle.setDirty", "platform.runtimeCapabilities", "platform.openExternal"]
)
def test_official_camel_case_capability_survives_public_import(boundary, capability):
    client, _ = boundary
    response = client.post(
        "/v1/plugins/import",
        files={"file": ("official.zip", capability_package(capability), "application/zip")},
    )
    assert response.status_code == 201, response.text
    catalog = client.get("/v1/workbench/catalog")
    assert catalog.status_code == 200
    item = next(
        row for row in catalog.json()["data"]["items"]
        if row["declaration"]["target"]["kind"] == "plugin"
    )
    assert item["declaration"]["compatibility"]["requiredCapabilities"] == [capability]


def test_unknown_capability_still_rejected_at_public_import(boundary):
    client, _ = boundary
    response = client.post(
        "/v1/plugins/import",
        files={"file": ("unknown.zip", capability_package("unknown.feature"), "application/zip")},
    )
    assert response.status_code == 400, response.text
