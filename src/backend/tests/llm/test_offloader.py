from tests.sandbox.runner_client import run_runner
"""Offloaded results remain readable in the same tool workspace."""

import base64
from dataclasses import replace
from pathlib import Path

import pytest
from agentscope.message import TextBlock, ToolResultBlock
from core.config.settings import settings
from core.llm.offloader import SandboxOffloader
from core.llm.tools import _paths
from services.script_runner_service import server


class RunnerFiles:
    """Use the real runner file endpoints and their session boundary."""

    async def put_file(self, session, path, content, user_id=None):
        await server.put_file(
            server.PutFileRequest(
                session_id=session,
                user_id=user_id,
                path=path,
                content_b64=base64.b64encode(content).decode("ascii"),
            )
        )

    async def get_file(self, session, path, user_id=None):
        result = await server.get_file(
            server.GetFileRequest(
                session_id=session,
                user_id=user_id,
                path=path,
            )
        )
        return base64.b64decode(result.content_b64)


@pytest.fixture(params=["local", "cloud"])
def runner_workspace(request, tmp_path, monkeypatch):
    root = tmp_path / "工作区 with spaces"
    monkeypatch.setenv("DEPLOY_PROFILE", "local" if request.param == "local" else "docker")
    monkeypatch.setattr(
        "core.config.local_mode.local_mode_enabled", lambda: request.param == "local"
    )
    monkeypatch.setattr(
        "core.config.settings.settings",
        replace(
            settings,
            sandbox=replace(settings.sandbox, provider="script_runner"),
        ),
    )
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(root))
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(root))
    return root


@pytest.mark.asyncio
async def test_offload_is_readable_in_tool_workspace(runner_workspace):
    storage = RunnerFiles()
    offloader = SandboxOffloader(storage, "chat-a")
    text = "完整输出\n带中文的结果\n"
    result = ToolResultBlock(id="call-1", name="bash", output=[TextBlock(text=text)])
    path = await offloader.offload_tool_result("agent-session", result)

    assert Path(path).parent == server._session_workspace("chat-a") / ".offload"
    assert await storage.get_file("chat-a", path) == text.encode("utf-8")
    assert _paths.to_physical_path(path, None, session_id="chat-a") == path
    with pytest.raises(server.HTTPException):
        await storage.get_file("chat-b", path)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_failed_offload_is_not_presented_as_a_file(runner_workspace, streaming):
    from agentscope.agent import ContextConfig
    from agentscope.message import UserMsg
    from agentscope.tool import Toolkit
    from agentscope.tool._response import ToolChunk
    from core.llm.compacting_agent import CompactingAgent
    from core.llm.tool_collector import ToolCollector
    from tests.llm.test_tool_result_images import CaptureModel

    class UnwritableFiles:
        async def put_file(self, *args, **kwargs):
            raise PermissionError("test-denied")

    async def logs():
        """Read diagnostic logs."""
        return ToolChunk(content=[TextBlock(text="BEGIN " + "log line " * 20_000 + " END")])

    collector = ToolCollector()
    collector.register_tool_function(logs)
    model = CaptureModel("logs", {})
    agent = CompactingAgent(
        name="agent",
        system_prompt="Inspect logs.",
        model=model,
        toolkit=Toolkit(tools=collector.function_tools),
        context_config=ContextConfig(tool_result_limit=100),
        offloader=SandboxOffloader(UnwritableFiles(), "chat-a"),
    )
    prompt = UserMsg(name="user", content="Inspect logs.")
    if streaming:
        async for _ in agent.reply_stream(prompt):
            pass
    else:
        await agent.reply(prompt)
    assert len(model.calls) == 2
    result = next(m["content"] for m in model.calls[-1] if m["role"] == "tool")
    assert "<<<TRUNCATED>>>" in result
    assert "未保存" in result
    assert "file in" not in result
    assert ".offload/" not in result
    assert "test-denied" not in result
    assert len(result) < 2_000


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_context_compaction_survives_offload_failure(monkeypatch, runner_workspace, shared):
    from types import SimpleNamespace

    from agentscope.agent import ContextConfig
    from agentscope.message import Msg
    from agentscope.tool import Toolkit
    from core.llm.compacting_agent import CompactingAgent
    from core.services import compaction_service
    from tests.llm.test_tool_result_images import CaptureModel

    class UnwritableFiles:
        async def put_file(self, *args, **kwargs):
            raise OSError("test-disk-full")

    async def summary(*args, **kwargs):
        return SimpleNamespace(content={"summary": "SUMMARY"})

    async def compact(chat_id, history):
        return [{"role": "user", "content": "SUMMARY"}]

    monkeypatch.setattr(
        "core.config.settings.settings",
        replace(
            settings,
            compaction=replace(settings.compaction, enabled=shared),
            sandbox=replace(settings.sandbox, provider="script_runner"),
        ),
    )
    monkeypatch.setattr(compaction_service, "run_mid_turn_compaction", compact)
    model = CaptureModel()
    model.context_size = 1_000
    model.generate_structured_output = summary

    async def count_tokens(*args, **kwargs):
        return 900

    model.count_tokens = count_tokens
    offloader = SandboxOffloader(UnwritableFiles(), "chat-a")
    agent = CompactingAgent(
        name="agent",
        system_prompt="Summarize.",
        model=model,
        toolkit=Toolkit(),
        context_config=ContextConfig(summary_template="{summary}"),
        offloader=offloader,
    )
    agent._jx_trigger_ratio = 0.8
    agent.state.context = [
        Msg(name="user", role="user", content=[TextBlock(text="old " * 10_000)]),
        Msg(name="agent", role="assistant", content=[TextBlock(text="previous answer")]),
        Msg(name="user", role="user", content=[TextBlock(text="continue")]),
    ]
    await agent.compress_context()

    visible = agent.state.summary + " ".join(m.get_text_content() for m in agent.state.context)
    assert "SUMMARY" in visible
    assert "old " * 100 not in visible
    assert "未保存" in visible
    assert "offloaded to" not in visible
    assert "test-disk-full" not in visible
    assert agent.offloader is offloader


@pytest.mark.asyncio
async def test_full_output_can_be_read_from_the_path_seen_by_model(runner_workspace):
    import re

    from agentscope.agent import ContextConfig
    from agentscope.message import UserMsg
    from agentscope.tool import Toolkit
    from agentscope.tool._response import ToolChunk
    from core.llm.compacting_agent import CompactingAgent
    from core.llm.tool_collector import ToolCollector
    from tests.llm.test_tool_result_images import CaptureModel

    text = 'BEGIN\n{"数据": "' + "line " * 30_000 + '"}\nEND'

    async def logs():
        """Read diagnostic logs."""
        return ToolChunk(content=[TextBlock(text=text)])

    collector = ToolCollector()
    collector.register_tool_function(logs)
    model = CaptureModel("logs", {})
    files = RunnerFiles()
    agent = CompactingAgent(
        name="agent",
        system_prompt="Inspect logs.",
        model=model,
        toolkit=Toolkit(tools=collector.function_tools),
        context_config=ContextConfig(tool_result_limit=100),
        offloader=SandboxOffloader(files, "chat-a", user_id="u1"),
    )
    await agent.reply(UserMsg(name="user", content="Inspect logs."))
    result = next(m["content"] for m in model.calls[-1] if m["role"] == "tool")
    path = re.search(r"完整工具输出已保存到文件：(.+?)。", result).group(1)
    assert await files.get_file("chat-a", path, user_id="u1") == text.encode("utf-8")
    # Read through the execution service as well: same cwd, same physical file.
    execution = await run_runner(
        server.ProcessRequest(
            session_id="chat-a",
            user_id="u1",
            language="python",
            script_name="verify.py",
            script_content="from pathlib import Path; print(Path(" + repr(path) + ").read_text())",
        )
    )
    assert execution.exit_code == 0, execution.stderr
    assert execution.stdout.strip() == text
    assert len(result) < 2_000


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["script_runner", "opensandbox", "cube"])
async def test_cloud_paths_use_provider_workspace(monkeypatch, provider_kind):
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    monkeypatch.setattr(
        "core.config.settings.settings",
        replace(
            settings,
            sandbox=replace(settings.sandbox, provider=provider_kind),
        ),
    )
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", "/workspace")
    writes = []

    class Files:
        async def put_file(self, session, path, content, user_id=None):
            writes.append((session, path, content, user_id))

    result = ToolResultBlock(id="call-1", name="bash", output=[TextBlock(text="data")])
    path = await SandboxOffloader(Files(), "chat-a", user_id="u1").offload_tool_result(
        "different-agent-session",
        result,
    )
    if provider_kind == "script_runner":
        assert path.startswith("/workspace/.sessions/")
    else:
        assert path.startswith("/workspace/.offload/")
    assert _paths.to_physical_path(path, "u1", session_id="chat-a") == path
    assert writes == [("chat-a", path, b"data", "u1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("root", [r"C:\Users\Test\工作区 with spaces", r"\\host\share\workspace"])
async def test_windows_offload_path_is_in_session_workspace(monkeypatch, root):
    import ntpath

    from services.script_runner_service.workspace_paths import session_root

    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", root)
    paths = []

    class Files:
        async def put_file(self, session, path, content, user_id=None):
            paths.append(path)

    # Never turn provider-controlled IDs into Windows filenames.
    result = ToolResultBlock(
        id="../bad\\name:<tag>",
        name="bash",
        output=[TextBlock(text="data")],
    )
    path = await SandboxOffloader(Files(), "chat-a").offload_tool_result("agent", result)
    assert ntpath.dirname(path) == ntpath.join(session_root(root, "chat-a"), ".offload")
    assert ntpath.basename(path).startswith("tool_")
    assert paths == [path]


@pytest.mark.asyncio
async def test_concurrent_results_and_context_do_not_overwrite(runner_workspace):
    import asyncio

    from agentscope.message import UserMsg

    files = RunnerFiles()
    offloader = SandboxOffloader(files, "chat-a")
    paths = await asyncio.gather(
        *(
            offloader.offload_tool_result(
                "agent",
                ToolResultBlock(id="same-call", name="bash", output=[TextBlock(text=value)]),
            )
            for value in ("first", "second")
        )
    )
    assert len(set(paths)) == 2
    assert [await files.get_file("chat-a", path) for path in paths] == [b"first", b"second"]
    path = await offloader.offload_context("agent", [UserMsg(name="user", content="history")])
    assert Path(path).parent == Path(paths[0]).parent
    assert await files.get_file("chat-a", path) == b"[user] history"


@pytest.mark.asyncio
async def test_offload_timeout_is_bounded_and_cancellation_propagates(
    monkeypatch, runner_workspace
):
    import asyncio

    from core.llm.offloader import OffloadError

    monkeypatch.setattr("core.llm.offloader._WRITE_TIMEOUT_SEC", 0.01)
    cancelled = asyncio.Event()

    class HangingFiles:
        async def put_file(self, *args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    result = ToolResultBlock(id="call", name="bash", output=[TextBlock(text="data")])
    offloader = SandboxOffloader(HangingFiles(), "chat-a")
    with pytest.raises(OffloadError):
        await asyncio.wait_for(offloader.offload_tool_result("agent", result), timeout=1)
    assert cancelled.is_set()

    class CancelledFiles:
        async def put_file(self, *args, **kwargs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await SandboxOffloader(CancelledFiles(), "chat-a").offload_tool_result("agent", result)


@pytest.mark.asyncio
@pytest.mark.parametrize("session", [None, ""])
async def test_no_file_is_published_without_a_persistent_workspace(session):
    from core.llm.offloader import OffloadError

    class Files:
        async def put_file(self, *args, **kwargs):
            pytest.fail("must not write to a shared or ephemeral root")

    with pytest.raises(OffloadError):
        await SandboxOffloader(Files(), session).offload_tool_result(
            "sdk-session-is-not-tool-session",
            ToolResultBlock(id="call", name="bash", output=[TextBlock(text="data")]),
        )


@pytest.mark.asyncio
async def test_framework_compaction_archives_partial_message(monkeypatch, runner_workspace):
    import re
    from types import SimpleNamespace

    from agentscope.agent import ContextConfig
    from agentscope.message import UserMsg
    from agentscope.tool import Toolkit
    from core.llm.compacting_agent import CompactingAgent
    from tests.llm.test_tool_result_images import CaptureModel

    monkeypatch.setattr(
        "core.config.settings.settings",
        replace(
            settings,
            compaction=replace(settings.compaction, enabled=False),
            sandbox=replace(settings.sandbox, provider="script_runner"),
        ),
    )
    model = CaptureModel()
    model.context_size = 1_000

    async def summary(*args, **kwargs):
        return SimpleNamespace(content={"summary": "SUMMARY"})

    model.generate_structured_output = summary
    counts = 0

    async def count_tokens(messages, tools=None, **kwargs):
        nonlocal counts
        counts += 1
        if counts == 1:
            return 900
        return sum(len(m.get_text_content()) for m in messages) // 4

    model.count_tokens = count_tokens
    files = RunnerFiles()
    agent = CompactingAgent(
        name="agent",
        system_prompt="Summarize.",
        model=model,
        toolkit=Toolkit(),
        context_config=ContextConfig(summary_template="{summary}"),
        offloader=SandboxOffloader(files, "chat-a"),
    )
    old = "old details " * 1_000
    agent.state.context = [
        UserMsg(
            name="user",
            content=[
                TextBlock(text=old),
                TextBlock(text="keep this recent detail"),
            ],
        )
    ]
    await agent.compress_context()

    assert len(agent.state.context) == 1
    assert agent.state.context[0].get_text_content() == "keep this recent detail"
    assert agent.state.summary.startswith("SUMMARY")
    path = re.search(r"offloaded to '(.+?)'", agent.state.summary).group(1)
    assert old.encode("utf-8") in await files.get_file("chat-a", path)
