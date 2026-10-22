"""Internal callbacks must work under the runner's actual clean environment."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core.jobs.callback as jobs_callback
import core.jobs.callback as callback
import pytest
from core.jobs.sdk import SDK_SOURCE
from core.sandbox.runner import server
from tests.sandbox.runner_client import run_spawn


@pytest.mark.parametrize("operation", ["probe", "progress"])
@pytest.mark.parametrize("target", ["loopback", "non_loopback"])
def test_loopback_callback_bypasses_system_proxy_in_real_runner(
    monkeypatch, tmp_path, operation, target
):
    hits = {"backend": 0, "proxy": 0}

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):
            hits["backend"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')

        def do_POST(self):
            hits["backend"] += 1
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":{"accepted":true}}')

        def log_message(self, *args):
            pass

    class RejectingProxy(BaseHTTPRequestHandler):
        def do_GET(self):
            hits["proxy"] += 1
            self.send_response(403)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), RejectingProxy)
    workers = []
    for endpoint in (backend, proxy):
        worker = threading.Thread(target=endpoint.serve_forever, daemon=True)
        worker.start()
        workers.append(worker)
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    base = (
        f"http://127.0.0.1:{backend.server_port}"
        if target == "loopback"
        else "http://callback.invalid"
    )
    # Model Windows registry discovery inside the actual clean child process.
    # No real system proxy settings are changed, and no inherited NO_PROXY is used.
    bootstrap = (
        "import os, urllib.request; "
        "assert not os.environ.get('NO_PROXY'); "
        "assert not os.environ.get('no_proxy'); "
        "urllib.request.proxy_bypass=lambda host: False; "
        f"urllib.request.getproxies=lambda: {{'http': 'http://127.0.0.1:{proxy.server_port}'}}; "
    )
    if operation == "probe":
        source = bootstrap + callback._PROBE_SOURCE
        args = [json.dumps([base])]
    else:
        source = (
            bootstrap
            + f"os.environ.update(JOB_ID='synthetic', JOB_TOKEN='test-only', JOB_CALLBACK_URL={base!r}); "
            + SDK_SOURCE
            + (
                "\nassert _post('log', {'message':'progress'}, timeout=1) == {'accepted':True}\n"
                "print('PROGRESS ACCEPTED')\n"
                if target == "loopback"
                else "\ntry:\n    _post('log', {'message':'progress'}, timeout=1)\n"
                "except JobError as error:\n    assert 'HTTP 403' in str(error)\n"
                "else:\n    raise AssertionError('proxy was bypassed')\n"
                "print('PROXY USED')\n"
            )
        )
        args = []
    try:
        result = asyncio.run(
            run_spawn(
                [server.INTERPRETERS["python"][0], "-c", source, *args],
                "{}",
                15,
                str(tmp_path),
            )
        )
        assert result["exit_code"] == 0, result
        if target == "loopback":
            expected = "PICK " + base if operation == "probe" else "PROGRESS ACCEPTED"
            assert hits == {"backend": 1, "proxy": 0}
        else:
            expected = "NONE" if operation == "probe" else "PROXY USED"
            assert hits == {"backend": 0, "proxy": 1}
        assert expected in result["stdout"], result
    finally:
        for endpoint in (backend, proxy):
            endpoint.shutdown()
            endpoint.server_close()
        for worker in workers:
            worker.join(timeout=2)


def test_probe_execution_failure_is_not_reported_as_network_failure(monkeypatch):
    monkeypatch.delenv("JOB_CALLBACK_URL", raising=False)

    async def missing_interpreter(*args, **kwargs):
        return 127, "", "private interpreter missing"

    monkeypatch.setattr(jobs_callback, "_sbx_bash", missing_interpreter)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(callback.resolve_callback_base(session_id="test", user_id="test"))
    message = str(error.value)
    assert "脚本执行失败" in message
    assert "127" in message
    assert "沙箱连不上后端" not in message
