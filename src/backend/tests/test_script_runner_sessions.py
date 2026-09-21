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


def test_file_endpoints_isolate_sessions(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    encoded = base64.b64encode(b"session-a").decode("ascii")

    asyncio.run(
        server.put_file(
            server.PutFileRequest(
                session_id="chat-a",
                path="report.txt",
                content_b64=encoded,
            )
        )
    )

    response = asyncio.run(
        server.get_file(
            server.GetFileRequest(
                session_id="chat-a",
                path="report.txt",
            )
        )
    )
    assert base64.b64decode(response.content_b64) == b"session-a"

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            server.get_file(
                server.GetFileRequest(
                    session_id="chat-b",
                    path="report.txt",
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
                    "from pathlib import Path\n" "Path('shared.txt').write_text('visible')\n"
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
                    "from pathlib import Path\n" "print(Path('shared.txt').read_text())\n"
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
                    "from pathlib import Path\n" "print(Path('shared.txt').exists())\n"
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


def test_concurrent_commands_keep_their_own_script_and_user_files(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    workspace = server._session_workspace("concurrent", create=True)
    original = workspace / "_bash.sh"
    original.write_text("user-owned script", encoding="utf-8")
    execute_process = server._execute_subprocess
    ready = 0
    gate = asyncio.Event()

    async def simultaneous_spawn(**kwargs):
        nonlocal ready
        ready += 1
        if ready == 4:
            gate.set()
        await gate.wait()
        return await execute_process(**kwargs)

    monkeypatch.setattr(server, "_execute_subprocess", simultaneous_spawn)

    async def run():
        return await asyncio.gather(
            *[
                server.execute(
                    server.ExecuteRequest(
                        session_id="concurrent",
                        language="bash",
                        script_name="_bash.sh",
                        script_content=f"printf task{index}",
                    )
                )
                for index in range(4)
            ]
        )

    results = asyncio.run(run())
    assert [(r.exit_code, r.stdout) for r in results] == [(0, f"task{index}") for index in range(4)]
    assert original.read_text() == "user-owned script"
    assert sorted(p.name for p in workspace.iterdir()) == ["_bash.sh"]


def test_container_skill_alias_is_private_and_executable(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOY_PROFILE", "docker")
    monkeypatch.setattr(server, "_subprocess_nproc_limit", lambda cmd: None)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    skills = tmp_path / ".skills_u" / "owner" / "demo"
    skills.mkdir(parents=True)
    (skills / "run.py").write_text("print('private-skill')")
    result = asyncio.run(
        server.execute(
            server.ExecuteRequest(
                session_id="skill-chat",
                user_id="owner",
                language="bash",
                script_name="skill.sh",
                script_content=f"{server.INTERPRETERS['python'][0]} /workspace/skills/demo/run.py",
            )
        )
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "private-skill"
    read = asyncio.run(
        server.get_file(
            server.GetFileRequest(
                session_id="skill-chat",
                user_id="owner",
                path="/workspace/skills/demo/run.py",
            )
        )
    )
    assert b"private-skill" in base64.b64decode(read.content_b64)
    with pytest.raises(HTTPException):
        asyncio.run(
            server.put_file(
                server.PutFileRequest(
                    session_id="skill-chat",
                    user_id="owner",
                    path="/workspace/skills/demo/run.py",
                    content_b64=base64.b64encode(b"overwrite").decode(),
                )
            )
        )


def test_container_shared_skill_link_is_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOY_PROFILE", "docker")
    monkeypatch.delenv("SANDBOX_SKILLS_DIR", raising=False)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    shared = tmp_path / "sandbox_skills" / "demo"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("shared-skill")
    view = tmp_path / ".skills_u" / "owner"
    view.mkdir(parents=True)
    (view / "demo").symlink_to(shared, target_is_directory=True)
    read = asyncio.run(
        server.get_file(
            server.GetFileRequest(
                session_id="shared-chat",
                user_id="owner",
                path="/workspace/skills/demo/SKILL.md",
            )
        )
    )
    assert base64.b64decode(read.content_b64) == b"shared-skill"
    with pytest.raises(HTTPException):
        asyncio.run(
            server.put_file(
                server.PutFileRequest(
                    session_id="shared-chat",
                    user_id="owner",
                    path="/workspace/skills/demo/SKILL.md",
                    content_b64=base64.b64encode(b"overwrite").decode(),
                )
            )
        )
    other = tmp_path / ".skills_u" / "someone-else"
    other.mkdir(parents=True)
    secret = other / "private.txt"
    secret.write_text("private")
    with pytest.raises(HTTPException):
        asyncio.run(
            server.get_file(
                server.GetFileRequest(
                    session_id="shared-chat",
                    user_id="owner",
                    path=str(secret),
                )
            )
        )
