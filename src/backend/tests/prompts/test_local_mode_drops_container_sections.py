"""桌面端不注入容器时代的 code_exec 两段，路径也不做改写。"""

from __future__ import annotations
from core.llm import prompt_rendering as prompt_rendering_module
from core.llm import prompt_versions as prompt_versions_module

import pytest


@pytest.mark.parametrize("local", [True, False])
def test_container_sections_only_ship_off_desktop(monkeypatch, local):
    import core.config.local_mode as local_mode
    from core.llm import prompt_versions as pvs

    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: local)
    monkeypatch.setattr(prompt_rendering_module, 'render_active_prompt', lambda *a, **k: None)
    monkeypatch.setattr(prompt_versions_module, 'get_active_version', lambda *a, **k: None)

    seg = prompt_rendering_module.render_kind_segment("code_exec")

    assert ("沙箱" in seg or "沙盒" in seg) or not local  # 非本机时两段仍在
    assert ("/workspace/" in seg) is (not local)


def test_real_paths_are_not_rewritten_in_local_mode(monkeypatch, tmp_path):
    """本机模式下模型给什么路径就是什么路径，不再映射到会话目录。"""
    monkeypatch.setattr("core.sandbox.workspace.paths.WORKSPACE", str(tmp_path))
    monkeypatch.setattr("core.sandbox.workspace.resolution.WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    from core.sandbox.workspace.resolution import to_physical_path

    real = str(tmp_path / ".sessions" / "abc" / "sites" / "demo")
    assert to_physical_path(real, "u1", session_id="chat-1") == real


def test_workspace_alias_is_gone():
    """路径别名已删除。"""
    from core.sandbox.workspace import resolution as _paths

    assert not hasattr(_paths, "canonicalize_ws_path")
