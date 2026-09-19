"""Ticket #05: desktop local-mode prompt section.

The local section is edition-agnostic shared code, only injected when
``project_is_local`` is set (desktop). These assertions pin the four
cloud-vs-local differences: workspace description, capability boundary, safety
language, and output location (no myspace upload).
"""

from __future__ import annotations

# prompt_runtime <-> project_section have a (pre-existing) import cycle that only
# resolves when prompt_runtime is imported first, as the app does. Do that before
# pulling the section helper so this test collects standalone.
import prompts.prompt_runtime  # noqa: F401
from prompts.project_section import _build_local_project_section


def test_local_section_describes_real_folder_and_workspace():
    s = _build_local_project_section(
        project_name="My Proj",
        project_instructions="",
        local_path="/Users/alice/proj",
        local_slug="my-proj-abc123",
    )
    assert "本地项目模式" in s
    assert "My Proj" in s
    # the real host path is surfaced as the working dir
    assert "/Users/alice/proj" in s


def test_local_section_says_no_upload():
    s = _build_local_project_section(
        project_name="P", project_instructions="", local_path="/tmp/p", local_slug="p-1"
    )
    # local mode must NOT tell the user to upload to My Space
    assert "上传到「我的空间」" not in s or "不" in s  # the only mention is the negation
    assert "不需要" in s and "上传" in s


def test_local_section_carries_safety_boundary():
    s = _build_local_project_section(
        project_name="P", project_instructions="", local_path="/tmp/p", local_slug="p-1"
    )
    assert "危险命令" in s or "越出授权目录" in s
    assert "快照" in s and "回滚" in s


def test_local_section_includes_project_instructions():
    s = _build_local_project_section(
        project_name="P",
        project_instructions="总是用中文回复",
        local_path="/tmp/p",
        local_slug="p-1",
    )
    assert "总是用中文回复" in s


def test_local_section_handles_missing_path_and_slug():
    s = _build_local_project_section(
        project_name="", project_instructions="", local_path="", local_slug=""
    )
    assert "本地项目模式" in s  # renders even without a resolved path


# ── 未绑项目时的默认工作根 ────────────────────────────────────────────────


def test_default_workspace_section_states_the_root():
    """没绑项目也有工作根，一句话说清在哪儿即可。"""
    from prompts.project_section import _build_default_workspace_section

    s = _build_default_workspace_section("/Users/alice/.hugagent/workspace/.sessions/abc")

    assert s == "当前处于默认工作目录 /Users/alice/.hugagent/workspace/.sessions/abc"


def test_default_workspace_section_is_empty_without_a_root():
    from prompts.project_section import _build_default_workspace_section

    assert _build_default_workspace_section("") == ""


def test_default_workspace_only_appears_in_local_mode_without_a_project(monkeypatch):
    """云端形态不注入；绑了项目也不注入（那时给的是项目文件夹）。"""
    import core.config.local_mode as local_mode
    from prompts.prompt_config import PromptConfig
    from prompts.prompt_runtime import build_system_prompt

    monkeypatch.setattr("core.sandbox._common.WORKSPACE", "/tmp/ws-under-test")

    for local, project_id, expect in ((True, None, True), (False, None, False), (True, "p1", False)):
        monkeypatch.setattr(local_mode, "local_mode_enabled", lambda local=local: local)
        ctx = {"chat_id": "chat-%s-%s" % (local, project_id)}
        if project_id:
            ctx["project_id"] = project_id
        prompt = build_system_prompt(PromptConfig(), ctx)
        assert ("当前处于默认工作目录" in prompt) is expect, (local, project_id)
        if expect:
            from services.script_runner_service.workspace_paths import session_root
            assert session_root("/tmp/ws-under-test", ctx["chat_id"]) in prompt


def test_desktop_prompt_and_bash_describe_effective_session(monkeypatch):
    from core.llm.tools import _paths
    from core.llm.tools.sandbox_tool import register_bash
    from prompts.prompt_config import PromptConfig
    from prompts.prompt_runtime import build_system_prompt
    from services.script_runner_service.workspace_paths import session_root

    root = "/tmp/中文 workspace"
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setattr("core.sandbox._common.WORKSPACE", root)
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", root)
    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    ctx = {"chat_id": "display-chat", "sandbox_session_id": "effective-session"}
    prompt = build_system_prompt(PromptConfig(), ctx)
    functions = {}

    class Toolkit:
        def register_tool_function(self, fn, **kwargs):
            functions[fn.__name__] = fn

    register_bash(Toolkit(), loader=None, loaded_skill_ids=set(), **ctx)
    description = functions["bash"].__doc__
    expected = session_root(root, "effective-session")
    assert expected in prompt and expected in description
    assert session_root(root, "display-chat") not in prompt + description
    assert root + "/skills" not in description
    assert "当前会话默认工作目录" in description


def test_cloud_bash_description_keeps_container_paths(monkeypatch):
    from core.llm.tools import _paths
    from core.llm.tools.sandbox_tool import register_bash

    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", "/workspace")
    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    functions = {}

    class Toolkit:
        def register_tool_function(self, fn, **kwargs):
            functions[fn.__name__] = fn

    register_bash(Toolkit(), loader=None, loaded_skill_ids=set(), chat_id="cloud-chat")
    doc = functions["bash"].__doc__
    assert "工作目录默认 /workspace" in doc
    assert "/workspace/skills" in doc
    assert ".sessions" not in doc
