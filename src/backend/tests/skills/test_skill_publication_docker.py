"""Opt-in real read-only mounts and Cube shell delivery; no application deployment."""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.skills.test_skill_publication import cloud, upload

IMAGE = os.getenv("SKILL_PUBLICATION_DOCKER_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Set an existing local Docker image for mount tests"
)


def docker(*args, data=None):
    result = subprocess.run(
        ["docker", *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def inspect_new_sandbox(view, shared):
    script = (
        "import json,pathlib;"
        "root=pathlib.Path('/workspace/skills');"
        "print(json.dumps({p.relative_to(root).as_posix():p.read_bytes().hex() "
        "for skill in root.iterdir() for p in skill.rglob('*') if p.is_file()}))"
    )
    output = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "1000:1000",
        "--mount",
        f"type=bind,source={view},target=/workspace/skills,readonly",
        "--mount",
        f"type=bind,source={shared},target=/workspace/skills_shared,readonly",
        "--entrypoint",
        "python",
        IMAGE,
        "-c",
        script,
    )
    return json.loads(output)


def test_new_readonly_sandbox_after_replace_and_delete_has_exact_files(cloud):
    from core.agent_skills.publication import prepare_skill_view

    upload(cloud.client, {"old.txt": "old", "nested/run.txt": "v1"})
    view = prepare_skill_view("alice")
    before = inspect_new_sandbox(view, cloud.storage)
    assert "sample/old.txt" in before
    upload(cloud.client, {"nested/run.txt": "v2", "asset.bin": b"\x00\xff"})
    after = inspect_new_sandbox(prepare_skill_view("alice"), cloud.storage)
    assert set(after) == {"sample/SKILL.md", "sample/nested/run.txt", "sample/asset.bin"}
    assert after["sample/nested/run.txt"] == b"v2".hex()
    assert after["sample/asset.bin"] == b"\x00\xff".hex()
    response = cloud.client.delete("/v1/me/skills/sample")
    assert response.status_code == 200
    assert inspect_new_sandbox(prepare_skill_view("alice"), cloud.storage) == {}


def test_cube_delivery_replaces_complete_tree_and_is_readable_by_nonroot(tmp_path, monkeypatch):
    from core.sandbox.cube_provider import CubeSandboxProvider

    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "skills"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.txt").write_text("old")
    container = (
        docker("run", "-d", "--rm", "--network", "none", "--entrypoint", "sleep", IMAGE, "infinity")
        .decode()
        .strip()
    )

    class Files:
        async def write(self, path, data):
            docker(
                "exec",
                "-i",
                "-u",
                "0",
                container,
                "sh",
                "-c",
                'cat > "$1"',
                "upload",
                path,
                data=data,
            )

    class Commands:
        async def run(self, command, **kwargs):
            docker("exec", "-u", "0", container, "sh", "-c", command)

    provider = CubeSandboxProvider.__new__(CubeSandboxProvider)
    provider._request_timeout_s = 0
    sandbox = SimpleNamespace(files=Files(), commands=Commands(), sandbox_id=container)
    try:
        docker("exec", "-u", "0", container, "mkdir", "-p", "/workspace")
        asyncio.run(provider._push_skill_dir(sandbox, "sample", source))
        (source / "old.txt").unlink()
        (source / "new.txt").write_text("new")
        asyncio.run(provider._push_skill_dir(sandbox, "sample", source))
        result = docker(
            "exec",
            "-u",
            "1000:1000",
            container,
            "sh",
            "-c",
            "test ! -e /workspace/skills/sample/old.txt && " "cat /workspace/skills/sample/new.txt",
        )
        assert result == b"new"
    finally:
        docker("rm", "-f", container)
