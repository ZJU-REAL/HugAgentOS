"""`hugagent-server engine` and the lightweight session client's entry/transport.

The engine contract is packages/cli/DESIGN.md §8.1.
"""

import asyncio
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import cli

BACKEND = Path(__file__).resolve().parents[1]


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def do_DELETE(self):
        self._reply()

    def _reply(self):
        status, body = self.routes.get((self.command, self.path.split("?")[0]), (404, b""))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    routes: dict = {}
    handler = type("Handler", (_Handler,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", routes
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = (tmp_path / "home").resolve()
    monkeypatch.setenv("HUGAGENT_HOME", str(path))
    return path


def _ready(url, home, pid=None):
    return {
        "version": 1,
        "url": url,
        "token": "sk-jx-test",
        "pid": os.getpid() if pid is None else pid,
        "started_at": "2026-10-24T00:00:00+00:00",
        "home": str(home),
    }


# ── ready file ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_ready_file_is_private_and_replaced_atomically(tmp_path):
    target = tmp_path / "nested" / "engine.json"
    target.parent.mkdir()
    target.write_text("old")
    target.chmod(0o644)
    payload = _ready("http://127.0.0.1:1234", tmp_path)

    cli._write_ready_file(target, payload)

    assert json.loads(target.read_text()) == payload
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(p.name for p in target.parent.iterdir()) == ["engine.json"]


def test_ready_file_is_removed_only_by_its_engine(tmp_path):
    mine, theirs = tmp_path / "mine.json", tmp_path / "theirs.json"
    cli._write_ready_file(mine, _ready("http://127.0.0.1:1", tmp_path))
    cli._write_ready_file(theirs, _ready("http://127.0.0.1:1", tmp_path, pid=os.getpid() + 1))

    cli._remove_ready_file(mine)
    cli._remove_ready_file(theirs)
    cli._remove_ready_file(tmp_path / "missing.json")

    assert not mine.exists() and theirs.exists()


def test_live_engine_needs_matching_home_live_pid_and_health(tmp_path, http_server):
    url, routes = http_server
    ready = tmp_path / "engine.json"
    routes[("GET", "/health")] = (200, b"{}")

    cli._write_ready_file(ready, _ready(url, tmp_path))
    assert cli._read_live_engine(ready, tmp_path)["url"] == url
    assert cli._read_live_engine(ready, tmp_path / "other") is None

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    cli._write_ready_file(ready, _ready(url, tmp_path, pid=dead.pid))
    assert cli._read_live_engine(ready, tmp_path) is None

    cli._write_ready_file(ready, _ready(url, tmp_path))
    routes[("GET", "/health")] = (503, b"{}")
    assert cli._read_live_engine(ready, tmp_path) is None

    cli._write_ready_file(ready, _ready("http://example.com:80", tmp_path))
    assert cli._read_live_engine(ready, tmp_path) is None


# ── command line ─────────────────────────────────────────────────────────────


def test_engine_arguments():
    args = cli.build_parser().parse_args(["engine"])
    assert (args.command, args.port, args.idle_timeout, args.ready_file) == ("engine", 0, 900, None)
    args = cli.build_parser().parse_args(
        ["engine", "--port", "4100", "--idle-timeout", "5", "--ready-file", "/x/e.json"]
    )
    assert (args.port, args.idle_timeout, args.ready_file) == (4100, 5, "/x/e.json")
    assert args.func is cli.cmd_engine


def test_engine_refuses_uninitialized_home(home, capsys):
    assert cli.main(["engine"]) == cli.EXIT_NOT_INITIALIZED == 3
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"error": "not_initialized"}
    # Nothing was started or created for the uninitialized home.
    assert not (home / "data.db").exists() and not (home / "engine.json").exists()


def test_engine_reuses_live_engine_of_this_home(home, http_server, capsys):
    url, routes = http_server
    routes[("GET", "/health")] = (200, b"{}")
    cli._write_ready_file(home / "engine.json", _ready(url, home))

    assert cli.main(["engine"]) == 0
    assert json.loads(capsys.readouterr().out) == _ready(url, home)


def test_engine_lock_is_exclusive(tmp_path):
    lock = cli._try_engine_lock(tmp_path / "engine.lock")
    assert lock is not None
    try:
        probe = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys; sys.path.insert(0, {str(BACKEND)!r})
                import cli
                from pathlib import Path
                print(cli._try_engine_lock(Path({str(tmp_path / 'engine.lock')!r})) is None)
            """)],
            capture_output=True, text=True, timeout=60,
        )
        assert probe.stdout.strip() == "True", probe.stderr
    finally:
        lock.close()


def test_activity_tracker_counts_requests_in_flight():
    gate = asyncio.Event()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            await gate.wait()

    tracker = cli._ActivityTracker(app)

    async def scenario():
        request = asyncio.ensure_future(tracker({"type": "http"}, None, None))
        await asyncio.sleep(0)
        assert tracker.in_flight == 1 and tracker.idle_for() == 0.0
        await tracker({"type": "lifespan"}, None, None)
        assert tracker.in_flight == 1
        gate.set()
        await request
        assert tracker.in_flight == 0

    asyncio.run(scenario())
    tracker.last_activity -= 30
    assert tracker.idle_for() >= 30


def test_engine_token_rotates_and_authenticates(tmp_path):
    """Each start mints a fresh `hugagent-cli` key and revokes the previous one."""
    script = textwrap.dedent("""
        import json, cli
        cli.apply_local_env(3999)
        from hugagent_cli.schema import ensure_schema_and_seed
        ensure_schema_and_seed()
        assert cli._engine_admin_user_id() is None
        user_id = cli.bootstrap_admin("admin", "Passw0rd!x")
        assert cli._engine_admin_user_id() == user_id
        import api.app  # noqa: F401  (composes the run services, as _run_engine does)
        first = cli.issue_engine_token(user_id)
        from core.auth.api_keys import resolve_api_key_identity
        from core.db.engine import SessionLocal
        from core.db.models import ChatRun, ChatSession
        from core.runs.journal import RunJournal
        db = SessionLocal()
        try:
            # A run the previous engine started with its key and left unfinished.
            _owner, first_key = resolve_api_key_identity(db, first)
            db.add(ChatSession(chat_id="left-over", user_id=user_id))
            db.commit()
            RunJournal(SessionLocal).accept(
                run_id="run-left-over", message_id="reply-left-over", chat_id="left-over",
                user_id=user_id, request_payload={"api_key_id": first_key.id},
            )
        finally:
            db.close()
        second = cli.issue_engine_token(user_id)
        db = SessionLocal()
        try:
            print(json.dumps({
                "first": resolve_api_key_identity(db, first) is not None,
                "second": resolve_api_key_identity(db, second) is not None,
                "prefix": second.startswith("sk-jx-"),
                "left_over": db.get(ChatRun, "run-left-over").status,
            }))
        finally:
            db.close()
    """)
    keep = ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "LANG")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(HUGAGENT_HOME=str(tmp_path / "home"), PYTHONPATH=str(BACKEND))
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "first": False, "second": True, "prefix": True, "left_over": "cancelled",
    }


# ── lightweight session client ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv, command",
    [
        (["serve"], "serve"),
        (["--port", "3002", "serve"], "serve"),
        (["--host", "0.0.0.0", "--port=3002", "onboard"], "onboard"),
        (["--host=127.0.0.1", "engine", "--port", "0"], "engine"),
        (["chat", "list"], None),
        (["--server", "http://x", "serve"], None),
        (["--port"], None),
        ([], None),
    ],
)
def test_session_client_finds_server_command_after_leading_options(argv, command):
    from hugagent_cli import main as client

    assert client.server_command(argv) == command


def test_session_client_points_server_commands_to_hugagent_server(capsys):
    from hugagent_cli import main as client

    assert client.main(["--port", "3002", "serve"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert "hugagent-server serve" in error and "lightweight client" in error


@pytest.mark.parametrize(
    "body, expected",
    [
        (b'{"detail": "Chat not found"}', "HugAgentOS HTTP 404: Chat not found"),
        (
            b'{"detail": {"code": 30001, "message": "Authorization required"}}',
            "HugAgentOS HTTP 404: Authorization required",
        ),
        (b'{"code": 40400, "message": "Gone\\nnow", "data": null}', "HugAgentOS HTTP 404: Gone now"),
        (b'{"detail": [{"loc": ["body"], "msg": "field required"}]}', "HugAgentOS HTTP 404: field required"),
        (b"<html>bad gateway</html>", "HugAgentOS HTTP 404: request refused"),
        (b"", "HugAgentOS HTTP 404: request refused"),
    ],
)
def test_transport_reports_server_error_detail(http_server, body, expected):
    from hugagent_cli.transport import Client

    url, routes = http_server
    routes[("GET", "/v1/x")] = (404, body)
    with pytest.raises(RuntimeError) as error:
        Client(url, "sk-jx-t").request("GET", "/v1/x")
    assert str(error.value) == expected


def test_transport_bounds_error_detail(http_server):
    from hugagent_cli.transport import DETAIL_LIMIT, Client

    url, routes = http_server
    routes[("GET", "/v1/x")] = (400, json.dumps({"detail": "y" * 5000}).encode())
    with pytest.raises(RuntimeError) as error:
        Client(url, "sk-jx-t").request("GET", "/v1/x")
    assert len(str(error.value)) == len("HugAgentOS HTTP 400: ") + DETAIL_LIMIT


def test_transport_empty_body_means_deleted_only_for_delete(http_server):
    from hugagent_cli.transport import Client

    url, routes = http_server
    for method in ("GET", "POST", "DELETE"):
        routes[(method, "/v1/x")] = (200, b"")
    client = Client(url, "sk-jx-t")
    assert client.request("DELETE", "/v1/x") == {"deleted": True}
    assert client.request("GET", "/v1/x") == {}
    assert client.request("POST", "/v1/x", {}) == {}


def test_activity_tracker_withdraws_ready_file_when_shutdown_begins():
    seen = []
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive():
        return next(messages)

    async def app(scope, receive, send):
        seen.append(((await receive())["type"], len(withdrawn)))
        seen.append(((await receive())["type"], len(withdrawn)))

    withdrawn = []
    tracker = cli._ActivityTracker(app, on_shutdown=lambda: withdrawn.append(1))
    asyncio.run(tracker({"type": "lifespan"}, receive, None))
    assert seen == [("lifespan.startup", 0), ("lifespan.shutdown", 1)]


def test_engine_startup_failure_does_not_use_onboarding_exit_code(tmp_path):
    # Exercise the process exit seen by the Node client, including cmd_engine's
    # preflight/lock and uvicorn's lifespan-failure SystemExit(3).
    script = textwrap.dedent("""
        import sys, cli, uvicorn
        from types import SimpleNamespace
        import hugagent_cli.schema
        sys.modules["api.app"] = SimpleNamespace(app=object())
        class FailedServer:
            started = False
            def __init__(self, config): pass
            def run(self, **kwargs): raise SystemExit(3)
        cli._is_initialized = lambda: True
        cli.apply_local_env = lambda port: None
        hugagent_cli.schema.ensure_schema_and_seed = lambda: None
        cli.bootstrap_default_plugins_if_needed = lambda: None
        cli._engine_admin_user_id = lambda: "test-admin"
        cli.issue_engine_token = lambda user_id: "fixture-token"
        uvicorn.Config = lambda *args, **kwargs: None
        uvicorn.Server = FailedServer
        cli.threading.Thread = lambda **kwargs: SimpleNamespace(start=lambda: None)
        sys.exit(cli.main(["engine"]))
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "HUGAGENT_HOME": str(tmp_path), "PYTHONPATH": str(BACKEND)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, result.stderr
    assert result.returncode != cli.EXIT_NOT_INITIALIZED
    assert not (tmp_path / "engine.json").exists()
    assert json.loads(result.stderr) == {"error": "startup_failed"}
