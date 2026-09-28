"""Real runner subprocess cwd and cleanup contracts."""

import asyncio
import os
from pathlib import Path

import pytest
from tests.sandbox.test_process_sessions import runner


async def start(runner, session, cwd=None, command="pwd; printf original > report.html"):
    body = dict(
        session_id=session,
        script_content=command,
        script_name="_bash.sh",
        language="bash",
        yield_time_ms=2000,
    )
    if cwd is not None:
        body["cwd"] = str(cwd)
    return await runner.post("/processes/start", json=body)


async def test_project_cwd_is_per_process_and_cleanup_keeps_project(runner, tmp_path):
    before = os.getcwd()
    roots = [tmp_path / "甲 project", tmp_path / "乙 project"]
    for root in roots:
        root.mkdir()
    replies = await asyncio.gather(
        *(start(runner, f"chat-{i}", root) for i, root in enumerate(roots))
    )
    for i, (root, response) in enumerate(zip(roots, replies)):
        assert response.status_code == 200, response.text
        assert response.json()["stdout"].strip() == str(root)
        assert (root / "report.html").read_text() == "original"
        assert sorted(p.name for p in root.iterdir()) == ["report.html"]
        response = await runner.post("/sessions/close", json={"session_id": f"chat-{i}"})
        assert response.status_code == 200, response.text
        assert (root / "report.html").read_text() == "original"
    assert os.getcwd() == before


@pytest.mark.parametrize("kind", ["missing", "file", "relative"])
async def test_invalid_explicit_cwd_never_falls_back(runner, tmp_path, kind):
    target = tmp_path / kind
    if kind == "file":
        target.write_text("not a directory")
    elif kind == "relative":
        target = Path("relative")
    response = await start(runner, "bad-cwd", target)
    assert response.status_code == 400, response.text
    assert not (tmp_path / "report.html").exists()


async def test_unbound_sessions_keep_persistent_separate_directories(runner):
    from services.script_runner_service import server

    first = await start(runner, "unbound-a")
    second = await start(runner, "unbound-b", command="pwd; test ! -e report.html")
    again = await start(runner, "unbound-a", command="cat report.html")
    assert first.json()["exit_code"] == second.json()["exit_code"] == again.json()["exit_code"] == 0
    assert first.json()["stdout"].strip() != second.json()["stdout"].strip()
    assert again.json()["stdout"] == "original"
    assert first.json()["stdout"].strip() == str(server._session_workspace("unbound-a"))


async def test_cloud_rejects_host_cwd_override(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_PROFILE", "cloud")
    response = await start(runner, "cloud", tmp_path)
    assert response.status_code == 400
    assert not (tmp_path / "report.html").exists()
