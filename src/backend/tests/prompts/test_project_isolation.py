"""Project path isolation and instruction-only prompt context."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from tests.prompts.test_prompt_cache_full_hash import isolated_prompt_database

ROOT = Path(__file__).resolve().parents[2]


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def paths():
    return _load("core/sandbox/workspace/resolution.py", "_paths_test_module")


class TestValidateProjectScopePath:
    def test_no_scope_passes_through(self, paths):
        # Non-project mode (folder_name=None) — any /myspace/ path is allowed
        assert paths.validate_project_scope_path("/myspace/anything/foo.md", None) is None
        assert paths.validate_project_scope_path("/myspace/x/y.txt", "") is None

    def test_in_scope_passes(self, paths):
        assert paths.validate_project_scope_path("/myspace/policies/q1.md", "policies") is None
        assert paths.validate_project_scope_path("/myspace/policies/sub/q1.md", "policies") is None

    def test_out_of_scope_rejected(self, paths):
        msg = paths.validate_project_scope_path("/myspace/other/x.md", "policies")
        assert msg is not None
        assert "policies" in msg
        assert "other" in msg

    def test_myspace_root_rejected_in_project_mode(self, paths):
        # In project mode, listing the /myspace root is not allowed
        msg = paths.validate_project_scope_path("/myspace", "policies")
        assert msg is not None

    def test_workspace_paths_always_allowed(self, paths):
        # The sandbox /workspace/ is scratch and not subject to project sandbox constraints
        assert paths.validate_project_scope_path("/workspace/scratch/x.py", "policies") is None
        assert paths.validate_project_scope_path("/workspace/foo", "policies") is None


@pytest.fixture
def project_prompt(monkeypatch):
    from prompts import project_section

    monkeypatch.setattr(project_section, "_load_db_prompt_parts", lambda: {})
    monkeypatch.setattr(project_section, "_get_project_mode_template", lambda: project_section._PROJECT_MODE_DEFAULT_TEMPLATE)
    return project_section


def test_project_prompt_retains_scope_and_instructions_without_inventory(project_prompt):
    result = project_prompt._build_project_section(
        project_name="测试项目", project_instructions="所有结论必须给出引用",
        folder_name="policies", folder_kind="personal",
        workspace_path="/workspace/myspace/policies",
    )
    assert "测试项目" in result
    assert "所有结论必须给出引用" in result
    assert "/workspace/myspace/policies" in result
    assert "按需查看" in result
    assert "项目沙盒文件清单" not in result
    assert "还没有任何文件" not in result


def test_custom_project_template_uses_scope_and_instructions(project_prompt, monkeypatch):
    monkeypatch.setattr(project_prompt, "_get_project_mode_template", lambda:
        "项目：{project_name}\n{folder_scope_block}\n{instructions_block}")
    result = project_prompt._build_project_section(
        project_name="P", project_instructions="Keep rules", folder_name="policies", folder_kind="personal",
    )
    assert "项目：P" in result
    assert "/myspace/policies/" in result
    assert "Keep rules" in result


def test_recovered_inventory_is_not_injected_into_system_prompt(monkeypatch, isolated_prompt_database):
    from prompts import project_section, prompt_runtime
    from prompts.prompt_config import PromptConfig, SystemPromptConfig

    monkeypatch.setattr(project_section, "_load_db_prompt_parts", lambda: {})
    result = prompt_runtime.build_system_prompt(
        PromptConfig(system_prompt=SystemPromptConfig(provider="inline", inline_template="BASE")),
        ctx={
            "project_id": "project-1", "project_name": "P", "project_instructions": "Keep rules",
            "project_folder_name": "policies", "project_folder_kind": "personal",
            "project_files": [{"name": "stale-inventory.txt", "size_bytes": 10}],
            "project_file_count": 1,
        },
    )
    assert "Keep rules" in result
    assert "stale-inventory.txt" not in result
    assert "项目沙盒文件清单" not in result
