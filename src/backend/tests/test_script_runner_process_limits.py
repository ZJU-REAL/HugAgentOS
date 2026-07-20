"""Regression tests for quick-install script-runner process management."""

from __future__ import annotations

import asyncio
import inspect
import signal
import sys
from pathlib import Path

from services.script_runner_service import server


def test_local_profile_skips_uid_wide_nproc_limit(monkeypatch):
    """The local runner must not cap every process owned by the login user."""
    monkeypatch.setenv("DEPLOY_PROFILE", "local")

    assert server._subprocess_nproc_limit(["bash"]) is None


def test_container_profile_keeps_nproc_limit(monkeypatch):
    """Container deployments retain the per-sandbox defence in depth."""
    monkeypatch.delenv("DEPLOY_PROFILE", raising=False)

    assert server._subprocess_nproc_limit(["bash"]) == 128
    assert server._subprocess_nproc_limit(["python3", "job.py"]) == 64


def test_local_safe_path_exposes_venv_and_office_skill_shims(monkeypatch, tmp_path):
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(skills_root))

    entries = server._local_safe_path_entries()

    assert str(Path(sys.executable).parent) in entries
    for skill_id in server._LOCAL_SKILL_CLI_IDS:
        assert str(skills_root / skill_id / "scripts") in entries


def test_timeout_kills_the_whole_process_group(monkeypatch, tmp_path):
    """A timed-out bash tree must not leave document-tool descendants alive."""
    monkeypatch.setenv("DEPLOY_PROFILE", "local")

    class FakeProcess:
        pid = 43210
        returncode = None

        async def wait(self):
            self.returncode = -signal.SIGKILL
            return self.returncode

        def kill(self):
            raise AssertionError("process-group kill should be used")

    proc = FakeProcess()
    spawn_kwargs = {}
    killed = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args
        spawn_kwargs.update(kwargs)
        return proc

    async def fake_wait_for(awaitable, timeout):
        del timeout
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise asyncio.TimeoutError

    def fake_killpg(pid, sig):
        killed.append((pid, sig))
        proc.returncode = -signal.SIGKILL

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(server.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(server.os, "killpg", fake_killpg)

    result = asyncio.run(server._execute_subprocess(["bash", "job.sh"], "{}", 1, str(tmp_path)))

    assert spawn_kwargs["start_new_session"] is True
    assert spawn_kwargs["preexec_fn"] is None
    assert killed == [(proc.pid, signal.SIGKILL)]
    assert proc.returncode == -signal.SIGKILL
    assert result == {"stdout": "", "stderr": "执行超时（1秒）", "exit_code": -1}


def test_success_uses_file_buffers_and_cleans_background_group(monkeypatch, tmp_path):
    """Inherited stdout descriptors must not turn a successful CLI into a timeout."""
    monkeypatch.setenv("DEPLOY_PROFILE", "local")

    class FakeProcess:
        pid = 43211
        returncode = None

        async def wait(self):
            self.returncode = 0
            return self.returncode

    proc = FakeProcess()
    killed = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args
        assert kwargs["stdin"] is not asyncio.subprocess.PIPE
        kwargs["stdin"].seek(0)
        assert kwargs["stdin"].read() == b'{"ok": true}'
        assert kwargs["stdout"] is not asyncio.subprocess.PIPE
        assert kwargs["stderr"] is not asyncio.subprocess.PIPE
        kwargs["stdout"].write(b"completed\n")
        kwargs["stderr"].write(b"warning\n")
        proc.returncode = 0
        return proc

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(server.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = asyncio.run(
        server._execute_subprocess(["bash", "job.sh"], '{"ok": true}', 1, str(tmp_path))
    )

    assert killed == [(proc.pid, signal.SIGKILL)]
    assert result == {"stdout": "completed\n", "stderr": "warning\n", "exit_code": 0}


def test_ce_installer_and_script_runner_ship_office_skill_runtime():
    repo_root = Path(__file__).resolve().parents[3]
    installer_path = repo_root / "ce" / "overlay" / "install.sh"
    if not installer_path.is_file():
        # In the generated public CE tree the overlay becomes the root installer.
        installer_path = repo_root / "install.sh"
    installer = installer_path.read_text(encoding="utf-8")
    dockerfile = (repo_root / "docker" / "Dockerfile.script-runner").read_text(encoding="utf-8")

    assert "pip_install -r docker/requirements-script-runner.txt" in installer
    assert '--prefix "${SKILL_NODE_DIR}" pptxgenjs playwright' in installer
    assert "apt-get download fonts-wqy-zenhei" in installer
    assert "install_libreoffice" in installer
    assert "wants_libreoffice_install" in installer
    assert "HUGAGENT_INSTALL_LIBREOFFICE" in installer
    assert "libreoffice-impress libreoffice-writer libreoffice-calc" in installer
    assert "PPT and Word previews" in installer
    assert "Continuing without LibreOffice" in installer
    assert '"JX_FONT_DIR": str(dd / "fonts")' in (
        repo_root / "src" / "backend" / "cli.py"
    ).read_text(encoding="utf-8")
    for command in ("word-cli", "excel-cli", "ppt-cli", "pdf-cli"):
        assert f"/usr/local/bin/{command}" in dockerfile
