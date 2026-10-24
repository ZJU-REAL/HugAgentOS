"""Packaging boundaries that a source-tree import alone cannot exercise."""

from pathlib import Path

from core.sandbox.providers.opensandbox import volumes

ROOT = Path(__file__).resolve().parents[4]


def test_opensandbox_controller_copy_sources_survive_restricted_context():
    dockerfile = ROOT / "docker/opensandbox-server/Dockerfile.opensandbox-server"
    rules = Path(f"{dockerfile}.dockerignore").read_text().splitlines()
    assert rules[0] == "**"
    included = {line[1:].rstrip("/") for line in rules if line.startswith("!")}
    for line in dockerfile.read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        source = Path(line.split()[1])
        assert (ROOT / source).is_file(), source
        # A negated file rule cannot restore files beneath an excluded parent.
        for item in (source, *source.parents):
            if item != Path("."):
                assert item.as_posix() in included, f"build context excludes {item}"


def test_skill_mount_requires_host_storage(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from core.sandbox.errors import SandboxError
    monkeypatch.setattr(volumes, "settings", SimpleNamespace(
        sandbox=SimpleNamespace(opensandbox_host_storage_path="")))
    with pytest.raises(SandboxError, match="HOST_STORAGE_PATH"):
        volumes._make_skills_volumes("user")
