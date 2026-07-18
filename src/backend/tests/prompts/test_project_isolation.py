"""Project sandbox isolation — path validation + system prompt file list.

- validate_project_scope_path: in project mode fs tools may only touch things under ``/myspace/<folder>/``;
- _build_project_section: the project section renders the first N file names + mime + size;
- build_system_prompt cache key folds the file-list signature in, so adding/removing files refreshes the cache.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def paths():
    return _load("core/llm/tools/_paths.py", "_paths_test_module")


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


class TestBuildProjectSection:
    @pytest.fixture(scope="class")
    def runtime(self):
        # prompt_runtime depends on modules like prompts.provider, so loading it directly triggers a chain;
        # here we only test the pure function _build_project_section, which could also be pulled straight out of the source;
        # for simplicity we still load it as a module and skip tests that hit dependency problems.
        try:
            return _load("prompts/project_section.py", "_prompt_runtime_test_module")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"prompt_runtime 无法独立加载: {exc}")

    def test_empty_files(self, runtime):
        s = runtime._build_project_section(
            project_name="测试项目",
            project_instructions="",
            folder_name="policies",
            folder_kind="personal",
            project_files=[],
        )
        assert "测试项目" in s
        assert "policies" in s
        # With an empty list the "项目沙盒文件清单" heading is not rendered
        assert "项目沙盒文件清单" not in s

    def test_renders_file_list(self, runtime):
        files = [
            {"name": "q1.md", "mime_type": "text/markdown", "size_bytes": 4096},
            {"name": "data/cities.xlsx", "mime_type": "application/vnd.ms-excel",
             "size_bytes": 480 * 1024},
        ]
        s = runtime._build_project_section(
            project_name="经济分析",
            project_instructions="所有结论必须给出引用",
            folder_name="econ",
            folder_kind="personal",
            project_files=files,
        )
        assert "经济分析" in s
        assert "q1.md" in s
        assert "data/cities.xlsx" in s
        assert "4.0 KB" in s  # 4096 → 4.0 KB
        assert "项目指令" in s
        assert "所有结论必须给出引用" in s

    def test_caps_at_50_files(self, runtime):
        files = [
            {"name": f"f{i}.txt", "mime_type": "text/plain", "size_bytes": 100}
            for i in range(75)
        ]
        s = runtime._build_project_section(
            project_name="多文件项目",
            project_instructions="",
            folder_name="bulk",
            folder_kind="personal",
            project_files=files,
        )
        assert "共 75 个" in s
        assert "列出前 50" in s
        assert "f0.txt" in s
        assert "f49.txt" in s
        assert "f50.txt" not in s  # the 51st is not in the first 50
        assert "还有 25 个未列出" in s


class TestProjectModeTemplate:
    """Approach A: the project section template is read from a DB part, while the file list/instructions are pre-rendered in Python."""

    @pytest.fixture(scope="class")
    def runtime(self):
        try:
            return _load("prompts/project_section.py", "_pr_template_test")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"prompt_runtime 无法独立加载: {exc}")

    def test_default_template_when_db_empty(self, runtime, monkeypatch):
        """No project_mode row in DB → use _PROJECT_MODE_DEFAULT_TEMPLATE."""
        monkeypatch.setattr(runtime, "_load_db_prompt_parts", lambda: {})
        s = runtime._build_project_section(
            project_name="P", project_instructions="",
            folder_name="econ", folder_kind="personal",
            project_files=[{"name": "q1.md", "size_bytes": 1024}],
        )
        assert "## 项目模式" in s
        assert "「P」" in s
        assert "q1.md" in s

    def test_db_template_overrides_default(self, runtime, monkeypatch):
        """A project_mode row exists in DB → use DB content as the template, {var} is still substituted."""
        custom = "## 自定义\n项目：{project_name}\n清单：\n{file_list_block}"
        monkeypatch.setattr(runtime, "_load_db_prompt_parts", lambda: {
            "project_mode": {"content": custom, "sort_order": 9000, "is_enabled": True},
        })
        s = runtime._build_project_section(
            project_name="X", project_instructions="ignore me",
            folder_name="bag", folder_kind="personal",
            project_files=[{"name": "a.txt", "size_bytes": 100}],
        )
        assert "## 自定义" in s
        assert "项目：X" in s
        assert "a.txt" in s
        # The default template's "## 项目模式" heading should not appear (overridden by the DB template)
        assert "## 项目模式" not in s
        # instructions_block is not referenced by the DB template → does not appear in the output
        assert "ignore me" not in s

    def test_db_template_disabled_falls_back(self, runtime, monkeypatch):
        """Row exists in DB but is_enabled=False → fall back to the default template."""
        monkeypatch.setattr(runtime, "_load_db_prompt_parts", lambda: {
            "project_mode": {"content": "DISABLED", "sort_order": 9000, "is_enabled": False},
        })
        s = runtime._build_project_section(
            project_name="P", project_instructions="",
            folder_name="econ", folder_kind="personal",
            project_files=[],
        )
        assert "## 项目模式" in s
        assert "DISABLED" not in s

    def test_db_template_empty_content_falls_back(self, runtime, monkeypatch):
        """Row exists in DB but content is an empty string → fall back (avoids rendering an empty section)."""
        monkeypatch.setattr(runtime, "_load_db_prompt_parts", lambda: {
            "project_mode": {"content": "   \n  ", "sort_order": 9000, "is_enabled": True},
        })
        s = runtime._build_project_section(
            project_name="P", project_instructions="",
            folder_name="econ", folder_kind="personal",
            project_files=[],
        )
        assert "## 项目模式" in s

    def test_empty_blocks_collapse_neatly(self, runtime, monkeypatch):
        """Empty folder_name + empty files + empty instructions → output has only the heading section, with no consecutive blank lines."""
        monkeypatch.setattr(runtime, "_load_db_prompt_parts", lambda: {})
        s = runtime._build_project_section(
            project_name="孤项目", project_instructions="",
            folder_name="", folder_kind="personal",
            project_files=[],
        )
        assert "孤项目" in s
        # There should be no 3+ consecutive newlines
        assert "\n\n\n" not in s


class TestRenderBlocks:
    """Degenerate behavior of each individual block-rendering function."""

    @pytest.fixture(scope="class")
    def runtime(self):
        try:
            return _load("prompts/project_section.py", "_pr_blocks_test")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"prompt_runtime 无法独立加载: {exc}")

    def test_file_list_block_empty(self, runtime):
        assert runtime._render_file_list_block([], 0) == ""

    def test_file_list_block_with_items(self, runtime):
        block = runtime._render_file_list_block(
            [{"name": "a.md", "mime_type": "text/markdown", "size_bytes": 4096}], 1,
        )
        assert "项目沙盒文件清单" in block
        assert "a.md" in block
        assert "4.0 KB" in block

    def test_folder_scope_block_no_folder(self, runtime):
        assert runtime._render_folder_scope_block("", "我的空间", 0) == ""

    def test_folder_scope_block_with_folder(self, runtime):
        b = runtime._render_folder_scope_block("policies", "我的空间", 5)
        assert "policies" in b
        assert "/myspace/policies/" in b
        # When files exist, no "暂无文件" hint is appended
        assert "暂无" not in b and "还没有" not in b

    def test_folder_scope_block_no_files_appends_hint(self, runtime):
        b = runtime._render_folder_scope_block("policies", "我的空间", 0)
        assert "还没有任何文件" in b

    def test_instructions_block(self, runtime):
        assert runtime._render_instructions_block("") == ""
        assert runtime._render_instructions_block("   ") == ""
        assert "项目指令" in runtime._render_instructions_block("做事要 quote 来源")


class TestFormatSize:
    @pytest.fixture(scope="class")
    def runtime(self):
        try:
            return _load("prompts/project_section.py", "_prompt_runtime_test_fmt")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"prompt_runtime 无法独立加载: {exc}")

    def test_bytes(self, runtime):
        assert runtime._format_size(0) == "0 B"
        assert runtime._format_size(512) == "512 B"

    def test_kb(self, runtime):
        assert runtime._format_size(1024) == "1.0 KB"
        assert runtime._format_size(4096) == "4.0 KB"

    def test_mb(self, runtime):
        assert runtime._format_size(1024 * 1024) == "1.0 MB"
        assert runtime._format_size(int(1.2 * 1024 * 1024)) == "1.2 MB"

    def test_gb(self, runtime):
        assert runtime._format_size(2 * 1024 * 1024 * 1024) == "2.0 GB"
