"""Desktop job callbacks use the same host and interpreter as the local runner."""

import asyncio
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core.jobs.callback as jobs_callback
import core.jobs.callback as callback
import pytest


@pytest.mark.parametrize("port_var", ["PORT", "BACKEND_PORT"])
def test_local_candidates_use_current_backend_port(monkeypatch, port_var):
    monkeypatch.delenv("JOB_CALLBACK_URL", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("BACKEND_PORT", raising=False)
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    monkeypatch.setenv("SANDBOX_PROVIDER", "script_runner")
    monkeypatch.setenv(port_var, "32201")
    assert callback.callback_base_candidates() == ["http://127.0.0.1:32201"]


def test_local_probe_reaches_backend_with_private_python(monkeypatch, tmp_path):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    private_dir = tmp_path / "private runtime with spaces"
    private_dir.mkdir()
    python = private_dir / "python"
    python.symlink_to(sys.executable)
    monkeypatch.delenv("JOB_CALLBACK_URL", raising=False)
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    monkeypatch.setenv("SANDBOX_PROVIDER", "script_runner")
    monkeypatch.setenv("PORT", str(server.server_port))
    monkeypatch.setenv("PY_BIN", str(python))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    async def host_bash(command, *, session_id, user_id, timeout):
        result = await asyncio.to_thread(
            subprocess.run,
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        assert result.returncode == 0, result.stderr
        return result.returncode, result.stdout, result.stderr

    monkeypatch.setattr(jobs_callback, "_sbx_bash", host_bash)
    try:
        assert asyncio.run(callback.resolve_callback_base(session_id="local", user_id="test")) == (
            f"http://127.0.0.1:{server.server_port}"
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
