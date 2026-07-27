"""Local mode rejects /myspace/ paths (My Space doesn't exist locally)."""

from __future__ import annotations

import core.config.local_mode as lm
from core.llm.tools._paths import validate_workspace_path


def test_myspace_rejected_in_local_mode(monkeypatch):
    monkeypatch.setattr(lm, "local_mode_enabled", lambda: True)
    err = validate_workspace_path("/myspace/notes.txt")
    assert err and "本机模式" in err
    # local workspace paths stay valid
    assert validate_workspace_path("/workspace/local/proj/a.py") is None
    assert validate_workspace_path("/workspace/scratch/t.txt") is None
    # real absolute host paths are accepted in local mode (host FS is the sandbox)
    assert validate_workspace_path("/Users/alice/Desktop/a.txt") is None
    assert validate_workspace_path("/Users/alice/proj/main.py") is None


def test_real_abs_paths_rejected_off_local_mode(monkeypatch):
    monkeypatch.setattr(lm, "local_mode_enabled", lambda: False)
    # cloud/web: arbitrary host paths are NOT accepted (only /myspace//workspace/)
    assert validate_workspace_path("/Users/alice/Desktop/a.txt") is not None


def test_myspace_allowed_off_local_mode(monkeypatch):
    monkeypatch.setattr(lm, "local_mode_enabled", lambda: False)
    # cloud/web behavior unchanged: /myspace/ is accepted
    assert validate_workspace_path("/myspace/notes.txt") is None
