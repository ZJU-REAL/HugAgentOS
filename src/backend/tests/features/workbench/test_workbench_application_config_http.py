"""Real session HTTP imports, admin configuration and owned HTTP upstream."""
import asyncio
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from core.auth import session
from core.config.settings import settings
from core.db.engine import Base, SessionLocal, engine
from core.db.models import UserShadow

HOST = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
ROOT = "/v1/workbench/admin/applications"


@pytest.fixture()
def boundary(monkeypatch):
    from api.app import app
    from features.workbench.ui import data_proxy
    from core.config.store.system_config import SystemConfigService
    assert settings.auth.mode == "session" and session._use_memory_store()
    assert not app.dependency_overrides
    monkeypatch.setenv("PLUGIN_RESOURCE_SECRET_KEY", "isolated-app-config-key")
    monkeypatch.setenv("EMAIL_SECRET_KEY", "isolated-api-key-encryption")
    session.process_state().MEMORY_SESSIONS.clear()
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for name in ("owner", "other", "admin", "content-admin"):
            db.add(UserShadow(user_id=name, user_center_id=name, username=name,
                              extra_data={"can_import_plugin": True,
                                          "can_system_config": name == "admin",
                                          "can_content_manage": name == "content-admin",
                                          "can_use_api_key": name == "admin"}))
        db.commit()
    clients = {}
    for name in ("owner", "other", "admin", "content-admin"):
        client = TestClient(app)
        token = asyncio.run(session.create_session({"user_id": name, "username": name}))
        client.cookies.set(settings.session.cookie_name, token)
        clients[name] = client
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"authorization": self.headers.get("Authorization"), "path": self.path})
            payload = b'{"ownedUpstream":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    data_proxy._client = None
    SystemConfigService._instance = None
    yield clients, "http://127.0.0.1:" + str(server.server_port), requests
    for client in clients.values():
        client.close()
    server.shutdown()
    server.server_close()
    worker.join()
    session.process_state().MEMORY_SESSIONS.clear()
    Base.metadata.drop_all(engine)
    SystemConfigService._instance = None
    data_proxy._client = None


def package(name="scoped-data", endpoint="endpoint", secret="token", url=None):
    from tests.features.workbench.workbench_plugin_support import package as base
    original = zipfile.ZipFile(io.BytesIO(base()))
    manifest = json.loads(original.read("plugin.json"))
    manifest["name"] = name
    ext = manifest["extensions"]["org.hugagent"]
    ext["admin_config"] = {"fields": [
        {"key": endpoint, "label": "Endpoint", "secret": False},
        {"key": secret, "label": "Token", "secret": True},
    ]}
    ui = ext["ui"]["contributes"]
    ui["modules"][0]["grants"] = ["data.query", "data_source:records"]
    ui["data_sources"] = [{
        "id": "records", "method": "GET", "url": url or "{config." + endpoint + "}/records",
        "auth": {"header": "Authorization", "value": "Bearer {config." + secret + "}"},
    }]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        archive.writestr("web/index.html", "<h1>Owned data</h1>")
    return output.getvalue()


def install(client, payload):
    response = client.post("/v1/plugins/import",
                           files={"file": ("data.zip", payload, "application/zip")})
    assert response.status_code == 201, response.text
    imported = response.json()["data"]
    catalog = client.get("/v1/workbench/catalog").json()["data"]["items"]
    item = next(row for row in catalog if row["declaration"]["target"]["kind"] == "plugin"
                and row["binding"]["installId"] == imported["install_id"])
    key = quote(item["applicationKey"], safe="")
    path = "/v1/workbench/applications/" + key + "/capabilities"
    instance = "scoped_configuration_instance_123"
    lease = client.post(path.removesuffix("/capabilities") + "/revision-leases", headers=HOST,
                        json={"revision": item["binding"]["packageRevision"], "instanceId": instance})
    assert lease.status_code == 200, lease.text
    response = client.post(path + "/approval", headers=HOST, json={
        "instanceId": instance,
        "revision": item["binding"]["packageRevision"],
        "capabilities": ["data.query", "data_source:records"],
    })
    assert response.status_code == 200, response.text
    body = {"revision": item["binding"]["packageRevision"], "instanceId": instance,
            "approvalId": response.json()["data"]["approvalId"],
            "sourceId": "records", "params": {}}
    return item, path, body


def configure(client, item, values=None, **extra):
    return client.put(ROOT + "/" + quote(item["applicationKey"], safe="") + "/config",
                      headers=HOST, json={"revision": item["binding"]["packageRevision"],
                                          "values": values or {}, **extra})


def test_real_admin_write_is_scoped_masked_and_revocation_blocks_data(boundary):
    clients, upstream, calls = boundary
    owner, admin = clients["owner"], clients["admin"]
    item, path, body = install(owner, package())
    assert owner.post(path + "/data/query", headers=HOST, json=body).status_code == 503
    assert not calls
    assert configure(owner, item, {"endpoint": upstream, "token": "scoped-secret"}).status_code in (401, 403)
    assert clients["other"].get(ROOT + "/configurable", headers=HOST).status_code in (401, 403)
    before = admin.get(ROOT + "/configurable", headers=HOST)
    assert before.status_code == 200, before.text
    metadata = next(row for row in before.json()["data"]["items"]
                    if row["applicationKey"] == item["applicationKey"])
    assert metadata["ownerUserId"] == "owner" and not metadata["configured"]
    assert all("value" not in field for field in metadata["fields"])
    response = configure(admin, item, {"endpoint": upstream, "token": "scoped-secret"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["configured"] is True
    assert "scoped-secret" not in response.text
    assert next(f for f in response.json()["data"]["fields"] if f["key"] == "token")["value"] == "****"
    for masked in ("****", ""):
        assert configure(admin, item, {"token": masked}).status_code == 200
    response = owner.post(path + "/data/query", headers=HOST, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"ownedUpstream": True}
    assert calls[-1]["authorization"] == "Bearer scoped-secret"
    count = len(calls)
    disabled = owner.patch("/v1/plugins/installed/" + item["binding"]["installId"] + "/enable",
                           json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    listing = admin.get(ROOT + "/configurable", headers=HOST).json()["data"]["items"]
    current = next(row for row in listing if row["applicationKey"] == item["applicationKey"])
    assert current["enabled"] is False and current["configured"] is True
    assert "scoped-secret" not in json.dumps(current)
    assert owner.post(path + "/data/query", headers=HOST, json=body).status_code in (404, 409)
    assert len(calls) == count
    assert owner.patch("/v1/plugins/installed/" + item["binding"]["installId"] + "/enable",
                       json={"enabled": True}).status_code == 200
    assert owner.delete(path + "/approval/" + body["approvalId"],
                        params={"revision": body["revision"]}, headers=HOST).status_code == 200
    assert owner.post(path + "/data/query", headers=HOST, json=body).status_code == 403
    assert len(calls) == count


def test_other_install_origin_revision_and_untrusted_target_cannot_inherit(boundary):
    clients, upstream, calls = boundary
    item, path, body = install(clients["owner"], package())
    assert configure(clients["admin"], item, {"endpoint": upstream, "token": "secret"}).status_code == 200
    other, other_path, other_body = install(clients["other"], package())
    assert other["applicationKey"] != item["applicationKey"]
    assert clients["other"].post(other_path + "/data/query", headers=HOST,
                                  json=other_body).status_code == 503
    assert not calls
    wrong = clients["admin"].put(ROOT + "/" + quote(item["applicationKey"], safe="") + "/config",
                                headers=HOST, json={"revision": "old", "values": {"token": "bad"}})
    assert wrong.status_code == 409
    wrong = configure(clients["admin"], item, {"outside": "bad"})
    assert wrong.status_code == 422
    wrong = clients["admin"].put(ROOT + "/" + quote(item["applicationKey"], safe="") + "/config",
                                headers={**HOST, "X-Workbench-Origin": "f" * 32},
                                json={"revision": body["revision"], "values": {"token": "bad"}})
    assert wrong.status_code == 409
    for headers in ({"Origin": "null", "Sec-Fetch-Site": "cross-site"}, {}):
        response = clients["admin"].put(ROOT + "/" + quote(item["applicationKey"], safe="") + "/config",
                                       headers=headers, json={"revision": body["revision"],
                                                              "values": {"token": "bad"}})
        assert response.status_code == 403
    hostile, hostile_path, hostile_body = install(clients["owner"],
        package(name="hostile-data", url=upstream.replace("127.0.0.1", "localhost") + "/records"))
    assert configure(clients["admin"], hostile, {"endpoint": upstream, "token": "secret"}).status_code == 200
    assert clients["owner"].post(hostile_path + "/data/query", headers=HOST,
                                json=hostile_body).status_code == 400
    assert not calls
    uninstall = clients["owner"].delete("/v1/plugins/installed/" + item["binding"]["installId"])
    assert uninstall.status_code == 200, uninstall.text
    fresh, fresh_path, fresh_body = install(clients["owner"], package())
    assert fresh["applicationKey"] != item["applicationKey"]
    assert clients["owner"].post(fresh_path + "/data/query", headers=HOST,
                                json=fresh_body).status_code == 503
    assert not calls


def test_private_manifest_cannot_read_or_trust_global_platform_fields(boundary):
    clients, upstream, calls = boundary
    from core.config.store.system_config import SystemConfigService
    service = SystemConfigService.get_instance()
    service.get("firecrawl.api_key")  # Seed only the platform's own built-in schema.
    response = clients["admin"].put("/v1/service-configs", json={"items": [
        {"key": "firecrawl.api_key", "value": "platform-only-secret"},
        {"key": "firecrawl.api_url", "value": upstream},
    ]})
    assert response.status_code == 200, response.text
    item, path, body = install(clients["owner"],
        package(endpoint="firecrawl.api_url", secret="firecrawl.api_key"))
    response = clients["owner"].post(path + "/data/query", headers=HOST, json=body)
    assert response.status_code == 503, response.text
    assert not calls
    response = configure(clients["admin"], item, {"firecrawl.api_url": upstream,
                                                 "firecrawl.api_key": "installation-only-secret"})
    assert response.status_code == 200, response.text
    response = clients["owner"].post(path + "/data/query", headers=HOST, json=body)
    assert response.status_code == 200, response.text
    assert calls[-1]["authorization"] == "Bearer installation-only-secret"
    service.invalidate_cache()
    assert service.get("firecrawl.api_key") == "platform-only-secret"
    assert "platform-only-secret" not in response.text



def test_real_admin_diagnostics_uses_full_origin_key_and_owner_receipt(boundary):
    clients, _, _ = boundary
    item, _, _ = install(clients["owner"], package())
    key = quote(item["applicationKey"], safe="")
    response = clients["owner"].post("/v1/workbench/applications/" + key + "/diagnostics",
                                      headers=HOST, json={"code": "bridge_timeout"})
    assert response.status_code == 200, response.text
    receipt = response.json()["data"]["diagnosticId"]
    assert clients["owner"].get("/v1/workbench/me/diagnostics/" + receipt).status_code == 200
    assert clients["other"].get("/v1/workbench/me/diagnostics/" + receipt).status_code in (403, 404)
    assert clients["owner"].get("/v1/workbench/admin/diagnostics").status_code in (401, 403)
    response = clients["admin"].get("/v1/workbench/admin/diagnostics",
                                    params={"user_id": "owner"})
    assert response.status_code == 200, response.text
    application = next(row for row in response.json()["data"]["applications"]
                       if row["applicationKey"] == item["applicationKey"])
    assert application["recentFailure"]["diagnosticId"] == receipt
    assert application["permissionStatus"] == "authorized"


def test_real_legacy_content_admin_authority_and_admin_api_key_denial(boundary):
    clients, upstream, _ = boundary
    item, path, _ = install(clients["owner"], package())
    content = clients["content-admin"]
    assert content.get("/v1/admin/plugins/installed").status_code == 200
    assert content.get("/v1/service-configs").status_code in (401, 403)
    listing = content.get(ROOT + "/configurable", headers=HOST)
    assert listing.status_code == 200, listing.text
    assert any(row["ownerUserId"] == "owner" for row in listing.json()["data"]["items"])
    response = configure(content, item, {"endpoint": upstream, "token": "content-admin-scoped"})
    assert response.status_code == 200, response.text
    assert "content-admin-scoped" not in response.text
    response = clients["admin"].post("/v1/me/api-keys", json={"name": "isolated-denied-admin-key"})
    assert response.status_code == 201, response.text
    issued_key = response.json()["data"]["api_key"]
    from api.app import app
    with TestClient(app) as key_client:
        headers = {**HOST, "Authorization": "Bearer " + issued_key}
        assert key_client.get(ROOT + "/configurable", headers=headers).status_code == 403
        response = key_client.put(ROOT + "/" + quote(item["applicationKey"], safe="") + "/config",
                                  headers=headers, json={"revision": item["binding"]["packageRevision"],
                                                         "values": {"endpoint": upstream}})
        assert response.status_code == 403
        response = key_client.post(path + "/approval", headers=headers,
                                   json={"revision": item["binding"]["packageRevision"],
                                         "capabilities": ["data.query"]})
        assert response.status_code == 403
