"""UI-only sample package driven through the public upload route."""

import io
import json
import zipfile


def package(entry="web/index.html", required=None, html="<h1>Package page</h1>", assets=None):
    app = {
        "schemaVersion": "1.0.0",
        "id": "reports",
        "title": "SDK reports",
        "target": {"kind": "plugin", "moduleId": "page"},
    }
    if required:
        app["compatibility"] = {"requiredCapabilities": required}
    manifest = {
        "name": "wb-sdk-reports",
        "version": "1.0.0",
        "extensions": {
            "org.hugagent": {
                "ui": {
                    "version": 3,
                    "contributes": {
                        "modules": [
                            {
                                "id": "page",
                                "surface": "workspace",
                                "entry": entry,
                                "title": "Reports",
                                "grants": [],
                            }
                        ],
                        "applications": [app],
                    },
                }
            }
        },
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("plugin.json", json.dumps(manifest))
        z.writestr("web/index.html", html)
        for path, data in (assets or {}).items():
            z.writestr(path, data)
    return output.getvalue()
