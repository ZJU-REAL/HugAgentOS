"""Direct Read/Edit operations feed project publication."""

import asyncio
import json
from dataclasses import replace

from fastapi.testclient import TestClient
from tests.features.hosted_apps.application_hosting_support import hosted  # noqa: F401
from tests.features.hosted_apps.test_application_hosting import data, table
from tests.features.hosted_apps.test_application_projects import projects  # noqa: F401


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

    def append(self, tool):
        function = tool._func
        kwargs = {"func_description": tool.description, "permission": getattr(tool, "dispatch_spec", None)}
        self.functions[function.__name__] = function


def test_immediate_file_tool_edit_is_published_without_registration_delay(
    projects, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from core.config.settings import settings
    from core.infra.ephemeral import LocalEphemeralState
    from core.llm.tools.edit_tool import register_edit
    from core.llm.tools.read_tool import register_read
    from core.files.spaces.access import personal_grant
    from core.files.storage import get_storage
    from core.projects.edition import ProjectScope

    config = replace(
        settings,
        storage=replace(settings.storage, path=str(tmp_path / "storage")),
        sandbox=replace(settings.sandbox, provider="opensandbox"),
    )
    monkeypatch.setattr("core.config.settings.settings", config)
    monkeypatch.setattr("core.config.settings.settings", config)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    ephemeral = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: ephemeral)

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
        root = get_storage().base_path / personal_grant("owner").prefix
        monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: FileTransport(root))
        scope = ProjectScope(
            editor["project_id"], "personal", project["linked_folder_id"], project["folder_name"]
        )
        toolkit = Toolkit()
        register_read(
            toolkit,
            user_id="owner",
            project_folder_name=project["folder_name"],
            scope=scope,
        )
        register_edit(
            toolkit,
            user_id="owner",
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
