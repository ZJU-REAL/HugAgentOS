"""Commands in a user's own folder get the user's environment; runner scratch keeps the clean one."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from core.sandbox.runner import user_env


def test_login_output_ignores_profile_noise_before_the_marker():
    raw = b"Welcome!\n" + user_env._MARKER.encode() + b"PATH=/opt/homebrew/bin:/usr/bin\0HOME=/Users/u\0MULTI=a\nb\0"
    assert user_env._parse_login_output(raw) == {
        "PATH": "/opt/homebrew/bin:/usr/bin", "HOME": "/Users/u", "MULTI": "a\nb",
    }
    assert user_env._parse_login_output(b"no marker at all") == {}


def test_credentials_and_backend_internals_never_follow():
    kept = user_env._filtered({
        "PATH": "/usr/bin", "SSH_AUTH_SOCK": "/tmp/agent", "LANG": "zh_CN.UTF-8",
        "OPENAI_API_KEY": "x", "GITHUB_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "x", "DB_PASSWORD": "x",
        "HUGAGENT_DESKTOP_BRIDGE_SECRET": "x", "DATABASE_URL": "x", "SANDBOX_RUNNER_URL": "x", "PWD": "/x",
    })
    assert kept == {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/tmp/agent", "LANG": "zh_CN.UTF-8"}


def test_only_folders_outside_the_runner_workspace_count_as_the_users(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".sessions" / "abc").mkdir(parents=True)
    project = tmp_path / "repo"
    project.mkdir()
    assert user_env.is_user_folder(project, str(workspace))
    assert not user_env.is_user_folder(workspace / ".sessions" / "abc", str(workspace))


@pytest.fixture
def user_login(monkeypatch):
    def install(environment):
        monkeypatch.setattr(user_env, "login_environment", lambda: dict(environment))
    return install


def test_user_path_wins_and_managed_tools_follow(tmp_path, user_login):
    user_login({"PATH": "/opt/homebrew/bin:/usr/bin", "HOME": "/Users/u", "NODE_PATH": "/u/node",
                "OFFICECLI_RESIDENT_FLUSH": "off"})
    managed = {"PATH": "/venv/bin:/usr/local/bin:/usr/bin", "HOME": "/Users/u", "PY_BIN": "/venv/bin/python",
               "NODE_PATH": "/caps/node_modules", "SITE_NODE_BASE": "/ws/.site-node", "XDG_CACHE_HOME": "/tmp/.cache",
               "OFFICECLI_RESIDENT_FLUSH": "each"}
    env = user_env.project_environment(managed, str(tmp_path / "workspace"), [str(tmp_path / "skills")])
    entries = env["PATH"].split(os.pathsep)
    if os.name != "nt":
        assert entries[0] == env[user_env.SHIM_DIR_ENV]
        entries = entries[1:]
    assert entries == ["/opt/homebrew/bin", "/usr/bin", "/venv/bin", "/usr/local/bin"]
    assert env["PY_BIN"] == "/venv/bin/python"
    assert env["SITE_NODE_BASE"] == "/ws/.site-node"
    assert env["NODE_PATH"] == os.pathsep.join(["/caps/node_modules", "/u/node"])
    assert env["XDG_CACHE_HOME"] == "/tmp/.cache"  # the user set none; the runner's stays
    assert env["OFFICECLI_RESIDENT_FLUSH"] == "each"  # the resident dies with each command


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim")
def test_shim_sends_skill_scripts_to_the_bundled_interpreter_and_the_rest_to_the_users(tmp_path, user_login):
    def fake(directory, label):
        directory.mkdir(parents=True)
        target = directory / "python3"
        target.write_text(f"#!/bin/sh\necho {label} \"$@\"\n")
        target.chmod(0o755)
        return target

    bundled = fake(tmp_path / "bundled", "BUNDLED")
    fake(tmp_path / "userbin", "USER")
    skills = tmp_path / "workspace" / "skills"
    (skills / "visualize" / "scripts").mkdir(parents=True)
    user_login({"PATH": f"{tmp_path / 'userbin'}:/usr/bin:/bin", "HOME": str(tmp_path)})
    env = user_env.project_environment(
        {"PATH": "/usr/bin:/bin", "PY_BIN": str(bundled)}, str(tmp_path / "workspace"), [str(skills)]
    )

    def run(*args):
        return subprocess.run(["python3", *args], env=env, capture_output=True, text=True, check=True).stdout.strip()

    script = str(skills / "visualize" / "scripts" / "render.py")
    assert run(script, "a.html") == f"BUNDLED {script} a.html"
    assert run("-u", script) == f"BUNDLED -u {script}"
    assert run("-m", "pytest", "-q") == "USER -m pytest -q"
    assert run(str(tmp_path / "repo" / "main.py")) == f"USER {tmp_path / 'repo' / 'main.py'}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim")
def test_shim_falls_back_to_the_bundled_interpreter_when_the_user_has_none(tmp_path, user_login):
    bundled = tmp_path / "bundled" / "python3"
    bundled.parent.mkdir()
    bundled.write_text("#!/bin/sh\necho BUNDLED\n")
    bundled.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    user_login({"PATH": str(empty), "HOME": str(tmp_path)})
    env = user_env.project_environment({"PATH": str(empty), "PY_BIN": str(bundled)}, str(tmp_path / "ws"), [])
    shim = os.path.join(env[user_env.SHIM_DIR_ENV], "python3")
    done = subprocess.run([shim, "x.py"], env=env, capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "BUNDLED"


@pytest.mark.skipif(os.name == "nt" or not os.path.exists("/bin/zsh") and not os.path.exists("/bin/bash"),
                    reason="needs a POSIX login shell")
def test_the_real_login_shell_answers(monkeypatch):
    user_env.login_environment.cache_clear()
    try:
        environment = user_env.login_environment()
    finally:
        user_env.login_environment.cache_clear()
    assert environment.get("PATH")
    assert not any(user_env._SECRET_NAME.search(name) for name in environment)
    assert sys.executable  # the capture never needs the backend's own interpreter
