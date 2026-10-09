"""Real Read/Edit -> filesystem registry -> project publication contract."""

import asyncio
import json
from dataclasses import replace

from fastapi.testclient import TestClient
from tests.api.application_hosting_support import hosted  # noqa: F401
from tests.api.test_application_hosting import data, table
from tests.api.test_application_projects import projects  # noqa: F401


class FileTransport:
    """Disposable filesystem adapter at the sandbox transport boundary."""

    def __init__(self, root):
        self.root = root

    def path(self, physical):
        prefix = "/workspace/myspace/owner/"
        assert physical.startswith(prefix)
        return self.root / physical[len(prefix) :]

    async def get_file(self, session, physical, **kwargs):
        return self.path(physical).read_bytes()

    async def put_file(self, session, physical, content, **kwargs):
        target = self.path(physical)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


class Toolkit:
    def __init__(self):
        self.functions = {}

    def register_tool_function(self, function, **kwargs):
        self.functions[function.__name__] = function


def test_immediate_file_tool_edit_is_published_without_registration_delay(
    projects, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from core.config.settings import settings
    from core.infra.ephemeral import LocalEphemeralState
    from core.llm.tools._state import ReadStateTracker
    from core.llm.tools.edit_tool import register_edit
    from core.llm.tools.read_tool import register_read
    from core.sandbox._common import myspace_cache_dir
    from core.services.project_scope import ProjectScope
    from core.space_sync.personal_registry import flush_user, start_registry, stop_registry

    config = replace(
        settings,
        storage=replace(settings.storage, path=str(tmp_path / "storage")),
        sandbox=replace(settings.sandbox, provider="opensandbox"),
    )
    monkeypatch.setattr("core.sandbox._common.settings", config)
    monkeypatch.setattr("core.config.settings.settings", config)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    ephemeral = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: ephemeral)

    @asynccontextmanager
    async def lifespan(app):
        await start_registry()
        try:
            yield
        finally:
            await stop_registry()

    projects.app.router.lifespan_context = lifespan
    with TestClient(projects.app) as client:
        app = data(client.post("/v1/applications", json={"title": "工具查询", "kind": "mcp"}))
        table(client, app["id"])
        tool = {
            "name": "find_entries",
            "description": "Original description",
            "table": "entries",
            "fields": ["name"],
            "filters": [],
        }
        data(client.post(f"/v1/applications/{app['id']}/mcp", json={"tools": [tool]}))
        editor = data(client.post(f"/v1/applications/{app['id']}/editor"))
        project = data(client.get(f"/v1/projects/{editor['project_id']}"))
        client.portal.call(flush_user, "owner")
        root = myspace_cache_dir("owner")
        monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: FileTransport(root))
        scope = ProjectScope(
            editor["project_id"], "personal", project["linked_folder_id"], project["folder_name"]
        )
        toolkit, tracker = Toolkit(), ReadStateTracker()
        register_read(
            toolkit,
            user_id="owner",
            state=tracker,
            project_folder_name=project["folder_name"],
            scope=scope,
        )
        register_edit(
            toolkit,
            user_id="owner",
            state=tracker,
            project_folder_name=project["folder_name"],
            scope=scope,
        )
        path = "/myspace/" + project["folder_name"] + "/mcp.json"

        async def edit():
            read = await toolkit.functions["Read"](path)
            assert "Original description" in str(read)
            result = await toolkit.functions["Edit"](
                path, "Original description", "Updated through Edit"
            )
            assert json.loads(result.content[0].text)["ok"]

        asyncio.run(edit())
        # No sleep or explicit flush between Edit and publication.
        receipt = data(client.post(f"/v1/applications/{app['id']}/mcp/project"))
        assert receipt["version"] == 2 and receipt["project_synced"]
        assert json.loads((root / project["folder_name"] / "mcp.json").read_text())["version"] == 2
        assert (
            data(client.get(f"/v1/applications/{app['id']}"))["tools"][0]["description"]
            == "Updated through Edit"
        )
