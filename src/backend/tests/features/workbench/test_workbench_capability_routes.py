"""Workbench capability HTTP authorization boundaries."""

import io
import json
import zipfile

from tests.features.workbench.test_workbench_plugin_routes import client, launched
from tests.features.workbench.workbench_plugin_support import package
from tests.db_support import share_factory


def capability_package():
    original = zipfile.ZipFile(io.BytesIO(package()))
    manifest = json.loads(original.read("plugin.json"))
    contributions = manifest["extensions"]["org.hugagent"]["ui"]["contributes"]
    contributions["modules"][0]["grants"] = ["data.query", "file.save", "data_source:records"]
    contributions["data_sources"] = [
        {"id": "records", "provider": "extensions.mcp_servers.wb09_fixture:records"}
    ]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        archive.writestr("web/index.html", "<h1>Capabilities</h1>")
    return output.getvalue()


def install(client):
    response = client.post(
        "/v1/plugins/import",
        files={"file": ("plugin.zip", capability_package(), "application/zip")},
    )
    assert response.status_code == 201, response.text
    return launched(client)


def test_opaque_origin_cannot_approve_or_self_grant(client):
    launch = install(client)
    endpoint = "/v1/workbench/applications/" + launch["applicationKey"] + "/capabilities"
    body = {"revision": launch["revision"], "instanceId": INSTANCE, "capabilities": ["file.save"]}
    assert (
        client.post(
            endpoint + "/approval",
            json=body,
            headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    response = client.post(
        endpoint + "/approval",
        json={**body, "approve": True},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 422


HOST = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
INSTANCE = "capability_http_instance_123"


def approve(client, launch, capabilities):
    endpoint = "/v1/workbench/applications/" + launch["applicationKey"] + "/capabilities"
    lease = client.post(endpoint.removesuffix("/capabilities") + "/revision-leases", headers=HOST,
                        json={"revision": launch["revision"], "instanceId": INSTANCE})
    assert lease.status_code == 200, lease.text
    result = client.post(
        endpoint + "/approval",
        json={"revision": launch["revision"], "instanceId": INSTANCE, "capabilities": capabilities},
        headers=HOST,
    )
    assert result.status_code == 200, result.text
    return endpoint, result.json()["data"]["approvalId"]


def test_approval_does_not_expand_and_revocation_blocks_file_writes(client):
    launch = install(client)
    endpoint, token = approve(client, launch, ["file.save"])
    rejected = client.post(
        endpoint + "/approval",
        json={"revision": launch["revision"], "instanceId": INSTANCE, "capabilities": ["chat.send"]},
        headers=HOST,
    )
    assert rejected.status_code == 403
    data = {"approvalId": token, "revision": launch["revision"], "instanceId": INSTANCE}
    response = client.post(
        endpoint + "/files/save",
        data=data,
        files={"file": ("wb09.txt", b"Real isolated content", "text/plain")},
        headers=HOST,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["size"] == 21
    assert "storage" not in response.text and "approvalId" not in response.text
    response = client.delete(
        endpoint + "/approval/" + token, params={"revision": launch["revision"], "instanceId": INSTANCE}, headers=HOST
    )
    assert response.status_code == 200
    assert (
        client.post(
            endpoint + "/files/save",
            data=data,
            files={"file": ("denied.txt", b"No", "text/plain")},
            headers=HOST,
        ).status_code
        == 403
    )


def test_approval_exact_identity_version_and_source_boundary(client):
    from core.auth.backend import UserContext, get_current_user

    launch = install(client)
    endpoint, token = approve(client, launch, ["data.query"])
    query = {
        "approvalId": token,
        "revision": launch["revision"], "instanceId": INSTANCE,
        "sourceId": "private",
        "params": {},
    }
    assert client.post(endpoint + "/data/query", json=query, headers=HOST).status_code == 403
    assert (
        client.post(
            endpoint + "/data/query", json={**query, "revision": "old"}, headers=HOST
        ).status_code
        == 409
    )
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="other", user_center_id="other", username="Other"
    )
    assert client.post(endpoint + "/data/query", json=query, headers=HOST).status_code == 404


def test_server_rate_budget_cannot_be_reset_by_new_http_requests(client):
    launch = install(client)
    endpoint, token = approve(client, launch, ["file.save"])
    for _ in range(30):
        response = client.post(
            endpoint + "/files/save",
            data={"approvalId": token, "revision": launch["revision"], "instanceId": INSTANCE},
            files={"file": ("../invalid", b"x", "text/plain")},
            headers=HOST,
        )
        assert response.status_code == 422, response.text
    response = client.post(
        endpoint + "/files/save",
        data={"approvalId": token, "revision": launch["revision"], "instanceId": INSTANCE},
        files={"file": ("blocked.txt", b"x", "text/plain")},
        headers=HOST,
    )
    assert response.status_code == 429, response.text


def test_controlled_file_resource_is_exact_selected_owner_and_revocable(client):
    from core.auth.backend import UserContext, get_current_user

    launch = install(client)
    endpoint, token = approve(client, launch, ["file.save"])
    data = {"approvalId": token, "revision": launch["revision"], "instanceId": INSTANCE}
    saved = client.post(
        endpoint + "/files/save",
        data=data,
        files={"file": ("resource.txt", b"Real resource", "text/plain")},
        headers=HOST,
    )
    assert saved.status_code == 200, saved.text
    file_id = saved.json()["data"]["fileId"]
    unselected = endpoint + "/files/" + file_id
    assert (
        client.get(
            unselected,
            params={"revision": launch["revision"], "instanceId": INSTANCE},
            headers={**HOST, "X-Workbench-Approval": token},
        ).status_code
        == 403
    )
    selected = client.post(
        endpoint + "/files/resource", json={**data, "fileId": file_id}, headers=HOST
    )
    assert selected.status_code == 200, selected.text
    url = selected.json()["data"]["resourceUrl"]
    response = client.get(
        url, headers={"Sec-Fetch-Site": "same-origin", "X-Workbench-Approval": token}
    )
    assert response.status_code == 200 and response.content == b"Real resource"
    assert (
        client.get(url, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"}).status_code
        == 403
    )
    assert (
        client.post(
            endpoint + "/files/resource", json={**data, "fileId": "not-owned"}, headers=HOST
        ).status_code
        == 404
    )
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="other", user_center_id="other", username="Other"
    )
    assert client.get(url, headers={**HOST, "X-Workbench-Approval": token}).status_code == 404


def test_per_operation_check_detects_policy_revocation(client, db_session):
    from core.db.models import UserShadow

    launch = install(client)
    endpoint, token = approve(client, launch, ["file.save"])
    body = {"revision": launch["revision"], "instanceId": INSTANCE, "approvalId": token, "capability": "file.save"}
    assert client.post(endpoint + "/check", json=body, headers=HOST).status_code == 200
    assert (
        client.post(
            endpoint + "/check", json={**body, "capability": "navigation.open"}, headers=HOST
        ).status_code
        == 403
    )
    row = db_session.get(UserShadow, "wb07")
    row.extra_data = {"allowed_apps": []}
    db_session.commit()
    assert client.post(endpoint + "/check", json=body, headers=HOST).status_code == 404


def test_simultaneous_approval_writes_do_not_grant_another_installation(client, db_session, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from core.db.engine import get_db
    from sqlalchemy.orm import sessionmaker

    launch = install(client)
    endpoint = "/v1/workbench/applications/" + launch["applicationKey"] + "/capabilities"
    factory = sessionmaker(bind=db_session.get_bind())

    def database():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = database
    share_factory(monkeypatch, factory)
    lease = client.post(endpoint.removesuffix("/capabilities") + "/revision-leases", headers=HOST,
                        json={"revision": launch["revision"], "instanceId": INSTANCE})
    assert lease.status_code == 200, lease.text

    def grant(_):
        return client.post(
            endpoint + "/approval",
            json={"revision": launch["revision"], "instanceId": INSTANCE, "capabilities": ["file.save"]},
            headers=HOST,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(grant, range(4)))
    assert all(r.status_code == 200 for r in responses)
    tokens = [r.json()["data"]["approvalId"] for r in responses]
    assert len(set(tokens)) == 4
    for token in tokens:
        assert (
            client.post(
                endpoint + "/check",
                json={
                    "revision": launch["revision"], "instanceId": INSTANCE,
                    "approvalId": token,
                    "capability": "file.save",
                },
                headers=HOST,
            ).status_code
            == 200
        )



def test_data_query_approved_source_and_inflight_revocation(client, db_session, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import quote

    from core.db.engine import get_db
    from sqlalchemy.orm import sessionmaker
    from tests.features.workbench.test_workbench_external_routes import AUTH

    entered, finish = threading.Event(), threading.Event()
    payload = b'{"rows":[{"id":7,"name":"Isolated fixture"}]}'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            finish.wait(5)
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
    try:
        original = zipfile.ZipFile(io.BytesIO(capability_package()))
        manifest = json.loads(original.read("plugin.json"))
        extension = manifest["extensions"]["org.hugagent"]
        extension["admin_config"] = {"fields": [
            {"key": "owned_endpoint", "label": "Owned endpoint", "secret": False}
        ]}
        extension["ui"]["contributes"]["data_sources"] = [{
            "id": "records", "url": "{config.owned_endpoint}/records", "method": "GET"
        }]
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("plugin.json", json.dumps(manifest))
            archive.writestr("web/index.html", "<h1>Capabilities</h1>")
        response = client.post("/v1/plugins/import",
            files={"file": ("http.zip", output.getvalue(), "application/zip")})
        assert response.status_code == 201, response.text
        launch = launched(client)
        monkeypatch.setenv("CONFIG_TOKEN", AUTH["Authorization"].removeprefix("Bearer "))
        configuration = client.put(
            "/v1/workbench/admin/applications/" + quote(launch["applicationKey"], safe="") + "/config",
            headers={**HOST, **AUTH}, json={"revision": launch["revision"],
                "values": {"owned_endpoint": "http://127.0.0.1:" + str(server.server_port)}})
        assert configuration.status_code == 200, configuration.text
        endpoint, token = approve(client, launch, ["data.query", "data_source:records"])
        factory = sessionmaker(bind=db_session.get_bind())

        def database():
            with factory() as db:
                yield db

        client.app.dependency_overrides[get_db] = database
        share_factory(monkeypatch, factory)
        body = {"approvalId": token, "revision": launch["revision"], "instanceId": INSTANCE,
                "sourceId": "records", "params": {}}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, endpoint + "/data/query", json=body, headers=HOST)
            assert entered.wait(5)
            assert client.delete(endpoint + "/approval/" + token,
                params={"revision": launch["revision"], "instanceId": INSTANCE}, headers=HOST).status_code == 200
            finish.set()
            assert pending.result().status_code == 403
        endpoint, fresh = approve(client, launch, ["data.query", "data_source:records"])
        response = client.post(endpoint + "/data/query",
                               json={**body, "approvalId": fresh}, headers=HOST)
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"rows": [{"id": 7, "name": "Isolated fixture"}]}
        assert client.post(endpoint + "/data/query",
            json={**body, "approvalId": fresh, "params": {"big": "x" * 80000}},
            headers=HOST).status_code == 413
    finally:
        finish.set()
        server.shutdown()
        server.server_close()
        worker.join()
