"""Management errors must remain actionable without exposing SQL payloads."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DataError, SQLAlchemyError

from core.services import cloud_management
from mcp_servers.management_registration import ArtifactSource, register


def invoke_install(monkeypatch, error):
    registered = {}
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(cloud_management, "install", fail)
    mcp = SimpleNamespace(add_tool=lambda fn, **kw: registered.update({kw["name"]: fn}))
    register(mcp, "skill-manager", lambda ctx: "owner")
    return asyncio.run(registered["install_skill"](ArtifactSource(kind="artifact", artifact_id="pkg")))


def test_database_error_is_actionable_and_hides_sql(monkeypatch):
    original = RuntimeError("private package contents")
    original.sqlstate = "22001"
    error = DataError("INSERT private_sql", {"secret": "private_parameter"}, original)
    result = invoke_install(monkeypatch, error)
    assert result["ok"] is False
    assert "22001" in result["error"]
    assert "length" in result["error"].lower()
    for private in ("private_sql", "private_parameter", "private package contents"):
        assert private not in str(result)


@pytest.mark.parametrize("detail", [None, [], {}, ""])
def test_empty_detail_does_not_hide_error(monkeypatch, detail):
    error = ValueError("invalid package")
    error.detail = detail
    assert invoke_install(monkeypatch, error)["error"] == "invalid package"


def test_http_detail_is_preserved(monkeypatch):
    result = invoke_install(monkeypatch, HTTPException(409, detail="revision_conflict"))
    assert result["error"] == "revision_conflict"


def test_unknown_database_error_is_not_empty_or_raw(monkeypatch):
    result = invoke_install(monkeypatch, SQLAlchemyError("private_sql"))
    assert "database" in result["error"].lower()
    assert "private_sql" not in result["error"]

def test_local_management_uses_same_safe_error(monkeypatch):
    import json
    from agentscope.message import ToolResultState
    from core.llm.management_tool import ManagementTool

    tool = ManagementTool("manager", SimpleNamespace(
        name="install_skill", description="Install", inputSchema={}
    ), "skill-manager", {}, cloud_backed=False)
    original = RuntimeError("private payload")
    original.pgcode = "22001"
    def fail(arguments):
        raise DataError("private SQL", {"secret": "private parameter"}, original)
    monkeypatch.setattr(tool, "_execute", fail)
    result = asyncio.run(tool(source={}))
    assert result.state == ToolResultState.ERROR
    payload = json.loads(result.content[0].text)
    assert "22001" in payload["error"]
    assert "private" not in payload["error"]
