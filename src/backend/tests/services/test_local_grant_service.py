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
    pol = svc.policy_for_gate()
    assert pol.disposition_for("delete") == "block"
    # unspecified category falls back to the built-in default
    assert pol.disposition_for("privilege") == "block"


def test_approval_mode_presets_drive_policy(tmp_path, monkeypatch):
    svc = _fresh_service(tmp_path, monkeypatch)
    assert svc.get_approval_mode() == "standard"
    # strict → everything blocks
    svc.set_approval_mode("strict")
    pol = svc.policy_for_gate()
    assert pol.out_of_scope == "block"
    assert pol.workspace_write == "block"
    assert pol.disposition_for("delete") == "block"
    # full → everything allows
    svc.set_approval_mode("full")
    pol = svc.policy_for_gate()
    assert pol.out_of_scope == "allow"
    assert pol.workspace_write == "allow"
    assert pol.disposition_for("privilege") == "allow"
    # invalid rejected
    try:
        svc.set_approval_mode("yolo")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_persistence_across_reload(tmp_path, monkeypatch):
    svc = _fresh_service(tmp_path, monkeypatch)
    svc.add_grant("/data/x", "read")
    svc2 = _fresh_service(tmp_path, monkeypatch)  # reload → reads the same file
    assert any(g["path"] == "/data/x" for g in svc2.list_grants())
