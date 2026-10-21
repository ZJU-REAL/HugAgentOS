"""Packaging boundaries that a source-tree import alone cannot exercise."""

from pathlib import Path

from core.sandbox.providers.opensandbox import volumes

ROOT = Path(__file__).resolve().parents[4]


def test_opensandbox_controller_copy_sources_survive_restricted_context():
    dockerfile = ROOT / "docker/Dockerfile.opensandbox-server"
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


def test_builtin_skill_mount_fallback_uses_central_extension_root(tmp_path, monkeypatch):
    backend = tmp_path / "src/backend"
    skills = backend / "extensions/skills"
    skills.mkdir(parents=True)
    monkeypatch.delenv("HOST_REPO_PATH", raising=False)
    monkeypatch.setattr(volumes, "SKILLS_DIR", skills)
    monkeypatch.setattr(volumes, "_BUILTIN_SKILLS_HOST_PATH", None)
    assert volumes._resolve_builtin_skills_host_path() == str(skills)
