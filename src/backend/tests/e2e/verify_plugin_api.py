"""Exercise plugin lifecycle and ownership via real session-authenticated HTTP.

Only use with the isolated core_external_server host; no production login.
"""

import io
import json
import os
import zipfile
from uuid import uuid4
from pathlib import Path
import httpx


def verify(base):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            ".claude-plugin/plugin.json",
            json.dumps(
                {
                    "name": "core-e2e-" + uuid4().hex[:8],
                    "version": "1.0.0",
                    "description": "Disposable integration fixture",
                }
            ),
        )
        bundle.writestr(
            "skills/check/SKILL.md",
            "---\nname: check\ndescription: Return the marker\n---\nReturn CORE_PLUGIN_OK.\n",
        )
    with httpx.Client(base_url=base, timeout=30) as client:
        assert client.post("/__test/login/core-owner").status_code == 200
        response = client.post(
            "/api/v1/plugins/import",
            files={"file": ("fixture.zip", archive.getvalue(), "application/zip")},
        )
        assert response.status_code == 201, response.text
        install_id = response.json()["data"]["install_id"]
        path = f"/api/v1/plugins/installed/{install_id}"
        try:
            assert client.get(path + "/detail").status_code == 200
            for enabled in (False, True):
                response = client.patch(path + "/enable", json={"enabled": enabled})
                assert response.status_code == 200, response.text
            client.post("/__test/login/core-other")
            response = client.delete(path)
            assert response.status_code in (400, 403, 404), response.text
            if response.status_code == 400:
                assert response.json()["code"] == 20001  # Existing cloud API contract.
        finally:
            client.post("/__test/login/core-owner")
            assert client.delete(path).status_code == 200
        assert client.get(path + "/detail").status_code == 404
    return dict(imported=True, detail=True, toggled=True, tenant_isolation=True, uninstalled=True)


if __name__ == "__main__":
    port = os.environ.get("CORE_E2E_PORT", "38273")
    result = verify("http://127.0.0.1:" + port)
    output = Path(os.environ["CORE_E2E_ROOT"])
    assert output.is_relative_to("/tmp")
    (output / "plugin-api.json").write_text(json.dumps(result, indent=2))
    print("PASS: plugin import, detail, toggles, tenant isolation, uninstall")
