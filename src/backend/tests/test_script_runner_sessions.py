from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi import HTTPException
from services.script_runner_service import server


def test_session_workspaces_are_stable_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))

    first = server._session_workspace("chat-a", create=True)
    again = server._session_workspace("chat-a", create=True)
    second = server._session_workspace("chat-b", create=True)

    assert first == again
    assert first != second
    assert first.parent == tmp_path / ".sessions"
    assert server._canon_ws("/workspace/report.txt", "chat-a") == str(first / "report.txt")


def test_file_endpoints_isolate_sessions(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    encoded = base64.b64encode(b"session-a").decode("ascii")

    asyncio.run(
        server.put_file(
            server.PutFileRequest(
                session_id="chat-a",
                path="/workspace/report.txt",
                content_b64=encoded,
            )
        )
    )

    response = asyncio.run(
        server.get_file(
            server.GetFileRequest(
                session_id="chat-a",
                path="/workspace/report.txt",
            )
        )
    )
    assert base64.b64decode(response.content_b64) == b"session-a"

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            server.get_file(
                server.GetFileRequest(
                    session_id="chat-b",
                    path="/workspace/report.txt",
                )
            )
        )
    assert exc_info.value.status_code == 404


def test_execute_reuses_files_only_inside_the_same_session(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))

    write_result = asyncio.run(
        server.execute(
            server.ExecuteRequest(
                session_id="chat-a",
                script_name="write.py",
                script_content=(
                    "from pathlib import Path\n"
                    "Path('/workspace/shared.txt').write_text('visible')\n"
                ),
            )
        )
    )
    same_session = asyncio.run(
        server.execute(
            server.ExecuteRequest(
                session_id="chat-a",
                script_name="read.py",
                script_content=(
                    "from pathlib import Path\n"
                    "print(Path('/workspace/shared.txt').read_text())\n"
                ),
            )
        )
    )
    other_session = asyncio.run(
        server.execute(
            server.ExecuteRequest(
                session_id="chat-b",
                script_name="probe.py",
                script_content=(
                    "from pathlib import Path\n" "print(Path('/workspace/shared.txt').exists())\n"
                ),
            )
        )
    )

    assert write_result.exit_code == 0
    assert same_session.stdout.strip() == "visible"
    assert other_session.stdout.strip() == "False"


def test_close_session_removes_only_the_target_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    first = server._session_workspace("chat-a", create=True)
    second = server._session_workspace("chat-b", create=True)
    (first / "a.txt").write_text("a", encoding="utf-8")
    (second / "b.txt").write_text("b", encoding="utf-8")

    result = asyncio.run(server.close_session(server.SessionRequest(session_id="chat-a")))

    assert result == {"closed": True}
    assert not first.exists()
    assert second.is_dir()
