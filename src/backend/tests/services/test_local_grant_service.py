"""Ticket #06: local folder grants + danger-command policy store.

Filesystem-backed, edition-agnostic shared service; tested in the main tree with
a temp HUGAGENT_HOME so it never touches the real data dir.
"""

from __future__ import annotations

import importlib
import os


def _fresh_service(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path))
    import core.services.local_grant_service as svc

    return importlib.reload(svc)


def test_add_list_remove_grant(tmp_path, monkeypatch):
    svc = _fresh_service(tmp_path, monkeypatch)
    assert svc.list_grants() == []
    svc.add_grant("~/proj", "readwrite")
    items = svc.list_grants()
    assert len(items) == 1
    expected = os.path.realpath(os.path.abspath(os.path.expanduser("~/proj")))
    assert items[0]["path"] == expected
    assert items[0]["mode"] == "readwrite"
    svc.remove_grant("~/proj")
    assert svc.list_grants() == []


def test_add_grant_is_idempotent_and_updates_mode(tmp_path, monkeypatch):
    svc = _fresh_service(tmp_path, monkeypatch)
    svc.add_grant("/data/a", "read")
    svc.add_grant("/data/a", "readwrite")
    items = svc.list_grants()
    assert len(items) == 1 and items[0]["mode"] == "readwrite"


def test_invalid_mode_rejected(tmp_path, monkeypatch):
    svc = _fresh_service(tmp_path, monkeypatch)
    try:
        svc.add_grant("/data/a", "sudo")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_policy_roundtrip_and_validation(tmp_path, monkeypatch):
    svc = _fresh_service(tmp_path, monkeypatch)
    saved = svc.set_policy({"delete": "block", "out_of_scope": "allow", "bogus": "x"})
    assert saved == {"delete": "block", "out_of_scope": "allow"}  # unknown key dropped
    assert svc.get_policy()["delete"] == "block"
    try:
        svc.set_policy({"network": "nuke"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_grants_and_policy_for_gate(tmp_path, monkeypatch):
    svc = _fresh_service(tmp_path, monkeypatch)
    svc.add_grant("/data/proj", "readwrite")
    svc.set_policy({"delete": "block"})
    gate_grants = svc.grants_for_gate()
    assert len(gate_grants) == 1 and gate_grants[0].path == "/data/proj"
    pol = svc.policy_for_gate("ask")
    assert pol.disposition_for("delete") == "block"
    # unspecified category falls back to the built-in default
    assert pol.disposition_for("privilege") == "block"


def test_chat_permission_preset_drives_the_local_policy(tmp_path, monkeypatch):
    """桌面端不再另存权限档：本机策略直接由聊天界面那一档翻译而来。"""
    svc = _fresh_service(tmp_path, monkeypatch)
    svc.set_policy({"delete": "block"})
    # 逐项确认 / 替我批准 → 走用户配置的分类处置
    for mode in ("ask", "auto"):
        pol = svc.policy_for_gate(mode)
        assert pol.out_of_scope == "confirm"
        assert pol.workspace_write == "allow"
        assert pol.disposition_for("delete") == "block"
    # 完全放开 → 全部放行
    pol = svc.policy_for_gate("full")
    assert pol.out_of_scope == "allow"
    assert pol.workspace_write == "allow"
    assert pol.disposition_for("privilege") == "allow"


def test_persistence_across_reload(tmp_path, monkeypatch):
    svc = _fresh_service(tmp_path, monkeypatch)
    svc.add_grant("/data/x", "read")
    svc2 = _fresh_service(tmp_path, monkeypatch)  # reload → reads the same file
    assert any(g["path"] == "/data/x" for g in svc2.list_grants())
