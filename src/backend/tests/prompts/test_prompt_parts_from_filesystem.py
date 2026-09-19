"""提示词正文的真源是 prompt_text 下的 md 文件，不是 Python 里的字符串常量。"""

from __future__ import annotations

from pathlib import Path

from core.services import prompt_version_service as pvs

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = BACKEND_ROOT / "prompts" / "prompt_text"


def test_system_reminder_convention_has_a_markdown_file():
    fp = PROMPT_TEXT / "default" / "system" / "05_system_reminder_convention.system.md"
    assert fp.is_file()
    assert "<system-reminder>" in fp.read_text(encoding="utf-8")


def test_system_part_sort_order_comes_from_the_filename_prefix():
    by_id = {p["part_id"]: p["sort_order"] for p in pvs._read_fs_parts("system")}
    assert by_id["system/00_role"] == 0
    # 新增的段落按文件名前缀落在 00_role 与 10_constraints 之间，不挤动相邻段。
    assert by_id["system/05_system_reminder_convention"] == 5
    assert by_id["system/10_constraints"] == 10
    assert by_id["system/40_format"] == 40


def test_plan_tool_segment_is_rendered_from_its_markdown_file(monkeypatch):
    fp = PROMPT_TEXT / "plan_tool" / "plan_tool.system.md"
    assert fp.is_file()

    from core.llm.plan_update_tool import build_plan_update_prompt_section

    # 未建库/未播种时退回文件系统，拿到的就是这份 md。
    monkeypatch.setattr(pvs, "render_active_prompt", lambda kind, db=None: None)
    assert build_plan_update_prompt_section().strip() == fp.read_text(encoding="utf-8").strip()


def test_plan_tool_segment_prefers_the_active_db_version(monkeypatch):
    from core.llm.plan_update_tool import build_plan_update_prompt_section

    monkeypatch.setattr(pvs, "render_active_prompt", lambda kind, db=None: "管理员改过的正文")
    assert build_plan_update_prompt_section() == "管理员改过的正文"


def test_plan_tool_is_a_builtin_kind_with_a_label():
    assert "plan_tool" in pvs.BUILTIN_KINDS
    assert any(k["key"] == "plan_tool" and k["builtin"] for k in pvs.all_kinds())


def test_new_filesystem_part_is_backfilled_into_an_existing_default_version(monkeypatch):
    """已建库的部署新增一个 md 文件后，要能补进现有的 default 版本。"""
    saved: dict = {}
    payload = {
        "active": {"system": "default"},
        "versions": [
            {
                "id": "default",
                "kind": "system",
                "name": "default",
                "parts": [{"part_id": "system/00_role", "content": "role", "sort_order": 0}],
            }
        ],
    }
    monkeypatch.setattr(pvs, "_load_payload", lambda db=None: payload)
    monkeypatch.setattr(
        pvs, "_save_payload", lambda p, db=None, updated_by="": saved.update({"payload": p})
    )

    result = pvs.seed_from_filesystem()

    parts = {p["part_id"] for p in saved["payload"]["versions"][0]["parts"]}
    assert "system/05_system_reminder_convention" in parts
    assert "system/40_format" in parts
    assert any("system/default:system/05_system_reminder_convention" in a for a in result["added"])


def test_no_prompt_body_is_hardcoded_in_python():
    runtime_src = (BACKEND_ROOT / "prompts" / "prompt_runtime.py").read_text(encoding="utf-8")
    plan_src = (BACKEND_ROOT / "core" / "llm" / "plan_update_tool.py").read_text(encoding="utf-8")
    assert "_SYSTEM_REMINDER_CONVENTION_DEFAULT" not in runtime_src
    assert "## 任务计划清单" not in plan_src


def test_every_builtin_kind_resolves_to_a_readable_directory():
    """加一个内置 kind 只需在 KIND_SPECS 加一行——这里保证那一行是自洽的。"""
    for kind, spec in pvs.KIND_SPECS.items():
        assert spec.layout in {"concat", "independent", "single"}
        assert pvs._fs_dir(kind).is_dir(), f"{kind} 的提示词目录不存在"
        assert pvs._read_fs_parts(kind), f"{kind} 目录里没有可读的 md"


def test_fs_parts_are_cached_until_the_files_change(tmp_path, monkeypatch):
    """兜底路径在每轮对话上，重复读同一批 md 是白费的磁盘 I/O。"""
    dirp = tmp_path / "plan_tool"
    dirp.mkdir()
    (dirp / "plan_tool.system.md").write_text("first", encoding="utf-8")
    monkeypatch.setattr(pvs, "_fs_dir", lambda kind: dirp)
    monkeypatch.setattr(pvs, "_fs_parts_cache", {})

    reads: list[str] = []
    real_build = pvs._build_fs_parts
    monkeypatch.setattr(
        pvs,
        "_build_fs_parts",
        lambda kind, d, files: (reads.append(kind), real_build(kind, d, files))[1],
    )

    assert pvs._read_fs_parts("plan_tool")[0]["content"] == "first"
    assert pvs._read_fs_parts("plan_tool")[0]["content"] == "first"
    assert len(reads) == 1, "文件没变却重读了磁盘"

    (dirp / "plan_tool.system.md").write_text("second content", encoding="utf-8")
    assert pvs._read_fs_parts("plan_tool")[0]["content"] == "second content"
    assert len(reads) == 2, "文件变了却没重读"


def test_read_fs_parts_hands_back_copies():
    """播种会把 part 直接塞进版本里，返回缓存本体会被就地改坏。"""
    first = pvs._read_fs_parts("plan_tool")
    first[0]["content"] = "mutated"
    assert pvs._read_fs_parts("plan_tool")[0]["content"] != "mutated"
