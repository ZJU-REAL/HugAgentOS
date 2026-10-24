"""Isolation must be actual containment, never a substring convention."""

import pytest
from tests.features.workbench.workbench_observer_paths import ROOT, guard_cli_binding, guard_environment, isolated_path


def test_observer_rejects_outside_paths_parent_traversal_and_symlinks(monkeypatch, tmp_path):
    name = "SCRIPT_RUNNER_WORKSPACE"
    safe = ROOT / ".scratch/workbench-19/isolation-test"
    monkeypatch.setenv(name, str(safe))
    assert isolated_path(name) == safe.resolve()
    for path in (
        "/tmp/workbench-19",
        str(ROOT / "storage/workbench-19"),
        str(ROOT / ".scratch/workbench-19/../workbench-19"),
        "workbench-19",
    ):
        monkeypatch.setenv(name, path)
        with pytest.raises(RuntimeError):
            isolated_path(name)
    link = tmp_path / "escape-link"
    link.symlink_to(ROOT / "src", target_is_directory=True)
    monkeypatch.setenv(name, str(link))
    with pytest.raises(RuntimeError):
        isolated_path(name)


def test_observer_requires_flag_token_and_loopback_binding(monkeypatch):
    import sys

    monkeypatch.setenv("STORAGE_PATH", str(ROOT / ".scratch/workbench-19/isolated-storage"))
    monkeypatch.delenv("WB19_METRICS_TESTONLY", raising=False)
    monkeypatch.delenv("SANDBOX_RUNNER_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        guard_environment("STORAGE_PATH")
    monkeypatch.setenv("WB19_METRICS_TESTONLY", "1")
    with pytest.raises(RuntimeError):
        guard_environment("STORAGE_PATH")
    monkeypatch.setenv("SANDBOX_RUNNER_TOKEN", "isolated-test-only-token")
    assert guard_environment("STORAGE_PATH").is_relative_to(ROOT / ".scratch")
    for host in ("0.0.0.0", "::1", "example.org"):
        monkeypatch.setattr(sys, "argv", ["uvicorn", "fixture:app", "--host", host])
        with pytest.raises(RuntimeError):
            guard_cli_binding()
    monkeypatch.setattr(sys, "argv", ["uvicorn", "fixture:app", "--host", "127.0.0.1"])
    guard_cli_binding()
