"""The desktop sends file bytes; the cloud owns hosted sites."""

import io
import json
import tarfile

from api.routes.v1 import desktop_capability as route
from core.services.desktop_gateway_uploads import UPLOAD_OPTIONS_HEADER, UPLOAD_SCHEMA_HEADER
from fastapi import FastAPI
from fastapi.testclient import TestClient


def bundle():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        body = b"<html>Built on the desktop</html>"
        item = tarfile.TarInfo("index.html")
        item.size = len(body)
        archive.addfile(item, io.BytesIO(body))
    return buf.getvalue()


def test_site_upload_requires_current_tool_grant(monkeypatch):
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route._require_capability_user] = lambda: "cloud-user"
    from core.services import desktop_capability

    monkeypatch.setattr(desktop_capability, "resolve_gateway_tool", lambda *a, **k: None)
    response = TestClient(app).post(
        "/v1/desktop/capability/gateway/site_publish/site-publish",
        content=bundle(),
        headers={
            UPLOAD_OPTIONS_HEADER: json.dumps({"title": "A site"}),
            UPLOAD_SCHEMA_HEADER: "a" * 64,
        },
    )
    assert response.status_code == 403


def test_uploaded_bytes_are_hosted_and_versioned_for_cloud_user(tmp_path, monkeypatch):
    import pytest
    from core.db.engine import Base
    from core.db.models import UserShadow
    from core.infra.exceptions import ResourceNotFoundError
    from core.services import desktop_capability
    from core.services.site_service import SiteService
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'cloud.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setattr(desktop_capability, "SessionLocal", factory)
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "cloud-storage"))
    with factory() as db:
        db.add(UserShadow(user_id="cloud-user", username="cloud-user"))
        db.commit()
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route._require_capability_user] = lambda: "cloud-user"
    monkeypatch.setattr(
        desktop_capability,
        "resolve_gateway_tool",
        lambda *a, **k: {"target": {}, "user_id": "cloud-user", "server_id": "site_publish"},
    )
    client = TestClient(app)
    options = {
        "title": "Desktop report",
        "slug": "desktop-report",
        "user_id": "forged-user",
        "chat_id": "local-chat",
        "project_id": "local-project",
    }
    headers = {UPLOAD_OPTIONS_HEADER: json.dumps(options), UPLOAD_SCHEMA_HEADER: "a" * 64}
    response = client.post(
        "/v1/desktop/capability/gateway/site_publish/site-publish",
        content=bundle(),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    published = json.loads(response.json()["data"]["content"][0]["text"])
    assert published["version"] == 1 and published["url"] == "/site/desktop-report/"
    with factory() as db:
        service = SiteService(db)
        site = service.get_owned(published["site_id"], "cloud-user")
        assert site.chat_id is None and site.project_id is None
        assert service.resolve_site_file(site, "")[0] == b"<html>Built on the desktop</html>"
        with pytest.raises(ResourceNotFoundError):
            service.get_owned(site.site_id, "forged-user")
    options["site_id"] = published["site_id"]
    headers[UPLOAD_OPTIONS_HEADER] = json.dumps(options)
    again = client.post(
        "/v1/desktop/capability/gateway/site_publish/site-publish",
        content=bundle(),
        headers=headers,
    )
    updated = json.loads(again.json()["data"]["content"][0]["text"])
    assert updated["site_id"] == published["site_id"] and updated["version"] == 2
    with factory() as db:
        listed, total = SiteService(db).list_sites("cloud-user", 1, 50)
        assert total == 1 and listed[0].site_id == published["site_id"]
    engine.dispose()


def test_archive_rejects_traversal_and_expansion_before_reading():
    import pytest
    from core.services.site_packaging import safe_extract_tar
    from core.services.site_service import MAX_SITE_FILE_BYTES

    for name, size in [("../escape", 1), ("/absolute", 1), ("huge.bin", MAX_SITE_FILE_BYTES + 1)]:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            entry = tarfile.TarInfo(name)
            entry.size = size
            archive.addfile(entry, io.BytesIO(b"x" * size))
        with pytest.raises(ValueError):
            safe_extract_tar(output.getvalue())


def test_gateway_transfers_local_build_bytes_and_returns_cloud_url(caps_root, monkeypatch):
    import asyncio

    import httpx
    import mcp.types
    from core.llm.mcp_manager import GatewayMCPTool
    from core.services import desktop_cloud_bridge as bridge
    from core.services.site_packaging import safe_extract_tar
    from tests.capabilities.test_runtime_recovery import state

    st = state("cloud-user")
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    archive = bundle()

    async def shell(*args, **kwargs):
        return 0, str(len(archive)), ""

    class Sandbox:
        async def get_file(self, *args, **kwargs):
            return archive

    monkeypatch.setattr("core.llm.tools._common.sandbox_exec_bash", shell)
    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: Sandbox())
    received = []

    def cloud(request):
        assert request.url.path.endswith("/site-publish")
        assert request.headers["authorization"] == "Bearer " + st["token"]
        assert json.loads(request.headers[UPLOAD_OPTIONS_HEADER])["title"] == "中文站点"
        received.extend(safe_extract_tar(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"ok": True, "site_id": "cloud-site", "url": "/site/report/"}
                            ),
                        }
                    ]
                }
            },
        )

    tool = GatewayMCPTool(
        mcp_name="site_publish",
        component="site_publish",
        tool=mcp.types.Tool(name="publish_site", inputSchema={"type": "object"}),
        invoke_url=st["cloud_base"] + "/api/v1/desktop/capability/gateway/site_publish/call",
        schema_hash="a" * 64,
        timeout=120,
        headers={"Authorization": "Bearer " + st["token"], "X-Current-User-Id": "local-user"},
        transport=httpx.MockTransport(cloud),
    )
    result = asyncio.run(tool(src_dir="/workspace/site", title="中文站点"))
    assert received == [("index.html", b"<html>Built on the desktop</html>")]
    published = json.loads(result.content[0].text)
    assert published["url"] == "https://cloud.example/site/report/"
