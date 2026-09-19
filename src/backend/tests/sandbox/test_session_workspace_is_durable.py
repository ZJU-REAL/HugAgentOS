"""会话工作目录不会被定时回收，执行也就在这个目录里进行。"""

from __future__ import annotations


def test_script_runner_has_no_session_reaping_endpoint():
    from services.script_runner_service import server

    routes = {getattr(r, "path", "") for r in server.app.routes}
    assert "/sessions/reap" not in routes
    assert not hasattr(server, "reap_idle_sessions")
    assert not hasattr(server, "ReapRequest")


def test_script_runner_provider_exposes_no_session_reaper():
    """周期回收器靠 ``getattr(provider, "reap_idle_sessions", None)`` 发现实现。"""
    from core.sandbox.script_runner_provider import ScriptRunnerProvider

    assert not hasattr(ScriptRunnerProvider, "reap_idle_sessions")
    # 显式销毁仍然保留：它由调用方在会话确实坏掉时主动触发，不是定时器。
    assert hasattr(ScriptRunnerProvider, "close_session")


def test_execution_runs_in_the_session_directory_itself(tmp_path, monkeypatch):
    """bash 的 cwd 就是会话工作目录本身，不是另开的临时子目录。"""
    import asyncio
    from services.script_runner_service import server

    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    captured = {}

    async def fake_exec(cmd, stdin_data, timeout, cwd, sandbox_launch=None):
        captured["cwd"] = cwd
        return {"success": True, "stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(server, "_execute_subprocess", fake_exec)
    req = server.ExecuteRequest(
        language="python", script_content="print(1)", script_name="s.py",
        session_id="chat-1", timeout=10,
    )
    asyncio.run(server.execute(req))

    expected = server._session_workspace("chat-1")
    assert captured["cwd"] == str(expected)
    # 会话目录本身必须还在——它不是临时目录
    assert expected.is_dir()
