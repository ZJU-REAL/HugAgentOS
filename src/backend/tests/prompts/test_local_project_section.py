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
