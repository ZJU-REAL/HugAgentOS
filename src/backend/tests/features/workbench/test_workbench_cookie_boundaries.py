"""Browser host spellings cannot bypass external cookie boundaries."""

import pytest
from tests.features.workbench.test_workbench_external_routes import APP, AUTH, BASE, client, create  # noqa: F401


def legacy(client, url):
    result = client.put(
        "/v1/content/app_config",
        headers=AUTH,
        json={
            "payload": {
                "apps": [
                    {"id": "canonical", "name": "Reviewed target", "enabled": True, "url": url}
                ]
            }
        },
    )
    assert result.status_code == 200, result.text
    return client.put(
        BASE + "/admin/external-auth/legacy:canonical",
        headers=AUTH,
        json={"revision": 0, "mode": "independent"},
    )


@pytest.mark.parametrize(
    "url", ["http://127.000.000.001", "http://0x7f000001", "http://2130706433", "http://0177.0.0.1"]
)
def test_ambiguous_numeric_browser_hosts_are_rejected(client, url):
    app = {**APP, "target": {"kind": "external", "mode": "browser", "url": url}}
    assert create(client, app).status_code == 422
    # Legacy declarations do not use the new JSON Schema. Their reviewed
    # authentication adapter must reject the same unsafe browser interpretation.
    assert legacy(client, url).status_code == 422


@pytest.mark.parametrize(
    "host,url",
    [("testserver", "http://testserver./app"), ("[::1]:9000", "http://[0:0:0:0:0:0:0:1]:9000/app")],
)
def test_equivalent_host_spellings_cannot_receive_host_cookies(client, host, url):
    # Legacy input preserves URLs that the protocol's stricter URI format can
    # reject. Keep testing that real compatibility boundary, not private helpers.
    assert legacy(client, url).status_code == 200
    result = client.post(
        BASE + "/applications/legacy:canonical/launch", headers={"Host": host}, json={}
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "host_cookie_scope_overlap"
