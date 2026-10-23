"""Exercise the runner with exactly the Python sources copied by its image."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[4]


def _image_sources(destination):
    dockerfile = (ROOT / "docker/sandbox/Dockerfile.script-runner").read_text()
    for line in dockerfile.splitlines():
        if not line.startswith("COPY src/backend/"):
            continue
        _, source, target = line.split()
        if not target.startswith("/app/"):
            continue
        source = ROOT / source
        target = destination / target.removeprefix("/app/")
        if source.is_dir():
            shutil.copytree(
                source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return dockerfile


@pytest.mark.parametrize("profile", ["local", "container"])
def test_packaged_runner_starts_and_executes_without_backend_dependencies(tmp_path, profile):
    source = tmp_path / "image"
    dockerfile = _image_sources(source)
    command = json.loads(
        next(line[4:] for line in dockerfile.splitlines() if line.startswith("CMD "))
    )
    assert command[3] == "core.sandbox.runner.server:app"
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from core.sandbox.runner import server; "
            "assert 'core.config.settings' not in sys.modules; "
            "assert 'sqlalchemy' not in sys.modules; "
            "assert 'core.sandbox.factory' not in sys.modules",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert probe.returncode == 0, probe.stderr
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    environment = {
        **os.environ,
        "DEPLOY_PROFILE": profile,
        "SCRIPT_RUNNER_WORKSPACE": str(tmp_path / "workspace"),
        "SANDBOX_RUNNER_TOKEN": "package-test-token",
        "PYTHONPATH": str(source),
    }
    # Use the same module and uvicorn arguments as the image and desktop launcher.
    argv = [sys.executable, *command[1:]]
    argv[argv.index("--host") + 1] = "127.0.0.1"
    argv[argv.index("--port") + 1] = str(port)
    with (tmp_path / "runner.log").open("w+") as log:
        runner = subprocess.Popen(argv, cwd=source, env=environment, stdout=log, stderr=log)
        try:

            def request(path, payload=None, *, authenticated=True):
                headers = {"Content-Type": "application/json"}
                if authenticated:
                    headers["Authorization"] = "Bearer package-test-token"
                data = None if payload is None else json.dumps(payload).encode()
                with urlopen(
                    Request(f"http://127.0.0.1:{port}{path}", data, headers), timeout=5
                ) as response:
                    return json.load(response)

            deadline = time.monotonic() + 15
            while True:
                try:
                    assert request("/health") == {"status": "ok"}
                    break
                except URLError:
                    if runner.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.05)
            with pytest.raises(HTTPError) as denied:
                request("/health", authenticated=False)
            assert denied.value.code == 401
            for language, code in [("python", "print('runner-ok')"), ("bash", "printf runner-ok")]:
                result = request(
                    "/processes/start",
                    {
                        "session_id": "packaged",
                        "language": language,
                        "script_name": "smoke",
                        "script_content": code,
                        "yield_time_ms": 1000,
                    },
                )
                while result["status"] == "running":
                    assert time.monotonic() < deadline
                    result = request(
                        "/processes/write",
                        {
                            "sandbox_session_id": "packaged",
                            "session_id": result["session_id"],
                            "yield_time_ms": 1000,
                        },
                    )
                assert result["exit_code"] == 0, result
                assert "runner-ok" in result["stdout"]
            assert request(
                "/put_file", {"session_id": "packaged", "path": "result.txt", "content_b64": "b2s="}
            )["ok"]
            assert (
                request("/get_file", {"session_id": "packaged", "path": "result.txt"})[
                    "content_b64"
                ]
                == "b2s="
            )
            assert request("/sessions/close", {"session_id": "packaged"})["closed"]
        finally:
            runner.terminate()
            try:
                runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner.kill()
                runner.wait(timeout=5)
