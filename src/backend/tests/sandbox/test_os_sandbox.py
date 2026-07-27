"""Tickets #09 / #12: OS-level sandbox command wrapping (macOS seatbelt / Linux bwrap).

Pure string transforms; no processes spawned. Loaded hermetically.
"""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(__file__)
_MOD = os.path.normpath(os.path.join(_HERE, "..", "..", "core", "sandbox", "os_sandbox.py"))
_spec = importlib.util.spec_from_file_location("os_sandbox", _MOD)
osx = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = osx
_spec.loader.exec_module(osx)  # type: ignore[union-attr]


def test_disabled_returns_command_unchanged(monkeypatch):
    monkeypatch.delenv("HUGAGENT_LOCAL_OS_SANDBOX", raising=False)
    assert osx.wrap_command("rm -rf build", ["/ws"], platform="macos") == "rm -rf build"


def test_macos_wrap_confines_writes(monkeypatch):
    monkeypatch.setenv("HUGAGENT_LOCAL_OS_SANDBOX", "1")
    out = osx.wrap_command("echo hi > a.txt", ["/Users/alice/proj"], platform="macos")
    assert "sandbox-exec -p" in out
    assert "(deny file-write*)" in out
    assert '(subpath "/Users/alice/proj")' in out
    assert '(subpath "/tmp")' in out  # baseline temp write
    assert "echo hi > a.txt" in out  # original command preserved in heredoc


def test_linux_wrap_uses_bwrap(monkeypatch):
    monkeypatch.setenv("HUGAGENT_LOCAL_OS_SANDBOX", "1")
    out = osx.wrap_command("touch x", ["/data/proj"], platform="linux")
    assert out.startswith("bwrap ")
    assert "--ro-bind / /" in out
    assert "--bind /data/proj /data/proj" in out
    assert "touch x" in out


def test_windows_platform_unchanged(monkeypatch):
    monkeypatch.setenv("HUGAGENT_LOCAL_OS_SANDBOX", "1")
    assert osx.wrap_command("dir", ["C:/x"], platform="windows") == "dir"


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("HUGAGENT_LOCAL_OS_SANDBOX", "1")
    assert osx.os_sandbox_enabled() is True
    monkeypatch.setenv("HUGAGENT_LOCAL_OS_SANDBOX", "0")
    assert osx.os_sandbox_enabled() is False
