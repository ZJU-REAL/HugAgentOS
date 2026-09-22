"""Desktop environment facts must match the runner and stay out of cloud prompts."""
import xml.etree.ElementTree as ET
import pytest

from prompts.prompt_config import PromptConfig
from prompts.prompt_runtime import build_system_prompt


@pytest.fixture(autouse=True)
def isolated_prompt_sources(monkeypatch, tmp_path):
    from prompts import prompt_runtime
    from core.services import prompt_version_service
    from pathlib import Path

    stock = Path(prompt_runtime.__file__).parent / "prompt_text/default/system/40_format.system.md"
    monkeypatch.setattr(prompt_runtime, "_load_db_prompt_parts", lambda: {})
    monkeypatch.setattr(prompt_runtime, "_get_db_prompt_version", lambda: "desktop-test")
    monkeypatch.setattr(prompt_version_service, "get_active_version", lambda kind, **kwargs: {
        "id": "desktop-test", "parts": [{"part_id": "system/40_format", "content": stock.read_text()}],
    } if kind == "system" else None)
    monkeypatch.setattr("core.services.local_grant_service._data_dir", lambda: tmp_path)
    prompt_runtime._prompt_cache.clear()


def test_environment_matches_execution_and_project_is_separate(monkeypatch, tmp_path):
    from services.script_runner_service import server
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setattr("core.sandbox._common.WORKSPACE", str(tmp_path))
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    ctx = {"chat_id": "display", "sandbox_session_id": "actual", "project_id": "p",
           "project_is_local": True, "project_name": "A & B", "project_local_path": "/tmp/A & B",
           "approval_mode": "ask", "now": "2030-01-02"}
    prompt = build_system_prompt(PromptConfig(), ctx)
    block = prompt[prompt.index("<environment_context>"):prompt.index("</environment_context>") + len("</environment_context>")]
    env = ET.fromstring(block)
    assert env.findtext("cwd") == str(server._session_workspace("actual"))
    assert env.findtext("project_root") == "/tmp/A & B"
    assert env.findtext("shell") == "bash"
    assert env.findtext("current_date") == "2030-01-02"
    assert env.find("filesystem/permission_profile").get("type") == "ask"
    assert "没 pin = 用户看不到" not in prompt
    assert "可直接在对话区下载" not in prompt
    assert "最高优先级，覆盖上文" not in prompt
    assert "在本机模式下**一律不适用，请忽略**" not in prompt


def test_cloud_has_no_desktop_environment(monkeypatch):
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    prompt = build_system_prompt(PromptConfig(), {"chat_id": "cloud-env"})
    assert "<environment_context>" not in prompt


def test_environment_refreshes_date_permissions_and_workspace_without_stale_cache(monkeypatch, tmp_path):
    from core.services import local_grant_service as grants
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setattr(grants, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr("core.sandbox._common.WORKSPACE", str(tmp_path / "work"))
    ctx = {"chat_id": "same", "approval_mode": "ask", "now": "2030-01-02"}
    first = build_system_prompt(PromptConfig(), ctx)
    grants.add_grant(str(tmp_path / "read & only"), "read")
    second = build_system_prompt(PromptConfig(), {**ctx, "approval_mode": "full", "now": "2030-01-03"})
    assert '<permission_profile type="ask">' in first
    assert '<permission_profile type="full">' in second
    assert '<current_date>2030-01-03</current_date>' in second
    assert 'mode="read"' in second and 'read &amp; only' in second
    assert "desktop_environment" not in ctx
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    assert "<environment_context>" not in build_system_prompt(PromptConfig(), ctx)


def test_windows_describes_actual_bash_not_powershell(monkeypatch):
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setattr("prompts.desktop_workspace.platform.system", lambda: "Windows")
    monkeypatch.setattr("core.sandbox._common.WORKSPACE", "C:/Users/Alice/work space")
    prompt = build_system_prompt(PromptConfig(), {"chat_id": "windows"})
    assert "<os>Windows</os>" in prompt
    assert "<shell>bash</shell>" in prompt
    assert "Windows 路径" in prompt
    assert "不要直接传入 PowerShell" not in prompt


def test_cloud_delivery_rules_are_preserved(monkeypatch):
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    prompt = build_system_prompt(PromptConfig(), {"chat_id": "cloud-delivery"})
    assert "没 pin = 用户看不到" in prompt
    assert "可直接在对话区下载" in prompt


def test_desktop_replaces_only_legacy_delivery_section():
    from prompts.desktop_workspace import desktop_prompt_text
    original = "角色规则\n### 输出约束（强制）\ncloud-only delivery\n## 当前时间\n2030-01-02\n### 自定义规则\nKeep this."
    result = desktop_prompt_text(original)
    assert "cloud-only delivery" not in result
    assert "角色规则" in result and "2030-01-02" in result and "Keep this." in result


def test_real_factory_modes_and_subagents_receive_environment(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
import asyncio
from types import SimpleNamespace
import core.db.models
from core.db.engine import Base, engine
from core.llm import agent_factory
from core.config import local_mode
Base.metadata.create_all(engine)
local_mode.local_mode_enabled = lambda: True
class EmptyLoader:
    def load_all_metadata(self): return {}
    def get_skill_dir(self, *args): return None
    def register_skills_to_toolkit(self, *args, **kwargs): return 0
agent_factory.get_skill_loader = lambda: EmptyLoader()
async def main():
    for extra in ({}, {"turbo_mode": True}, {"user_agent": SimpleNamespace(
        agent_id="test-agent", system_prompt="Answer clearly", name="Test", description="Test",
        tools=[], skills=[], model_config={}, extra_data={},
    )}):
        agent, clients = await agent_factory.create_agent_executor(
            disable_tools=True, max_iters=1, chat_id="test-context", approval_mode="full", **extra)
        text = agent._jx_compaction_system_prompt
        assert text.count("<environment_context>") == 1, text
        assert '<permission_profile type="full">' in text
        assert "<shell>bash</shell>" in text
        for client in clients: await client.close()
    from prompts.desktop_templates import desktop_version
    with desktop_version({"parts": [{"part_id": "guidance", "content": "UNIQUE_GUIDANCE"}]}):
        agent, clients = await agent_factory.create_agent_executor(
            disable_tools=True, max_iters=1, chat_id="disabled-env", approval_mode="full")
        assert agent._jx_compaction_system_prompt.count("UNIQUE_GUIDANCE") == 1
        for client in clients: await client.close()
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'factory.db'}",
             "REDIS_URL": "", "SANDBOX_TOOLS_ENABLED": "false",
             "HUGAGENT_CAPS_ROOT": str(tmp_path / "caps")},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_non_windows_prompt_omits_windows_guidance(monkeypatch, system):
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setattr("prompts.desktop_workspace.platform.system", lambda: system)
    prompt = build_system_prompt(PromptConfig(), {"chat_id": "platform-guidance"})
    assert f"<os>{system}</os>" in prompt
    assert "<shell>bash</shell>" in prompt
    assert "Windows" not in prompt
    assert "Git Bash" not in prompt
    assert "PowerShell" not in prompt
    assert "C:/..." not in prompt


@pytest.mark.parametrize("project", [False, True])
def test_site_records_and_rules_are_not_ambient_context(monkeypatch, project):
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    ctx = {"chat_id": "site-free", "local_site_edit": "LEGACY_SITE_RECORD"}
    if project:
        ctx.update(project_id="p", project_is_local=True,
                   project_name="Example", project_local_path="/tmp/example")
    prompt = build_system_prompt(PromptConfig(), ctx)
    assert "LEGACY_SITE_RECORD" not in prompt
    assert "publish_site" not in prompt
    assert "sites/<站点名>" not in prompt
    if project:
        assert "/tmp/example" in prompt


def test_missing_project_path_remains_generic():
    from prompts.project_section import _build_local_project_section
    prompt = _build_local_project_section(project_name="Example", project_instructions="", local_path="", local_slug="")
    assert "Example" in prompt
    assert "确认路径" in prompt
    assert "list_sites" not in prompt
