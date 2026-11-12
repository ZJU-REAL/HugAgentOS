"""The device link's session operations, end to end over a real loopback socket.

The cloud Personal Agent runs session operations on a desktop through
``personal_agent.device_link``: the desktop's own backend calls its loopback HTTP
API as the signed-in cloud account, with the desktop bridge headers. Every other
test of that chain replaces ``local_api.execute``, ``_bridge_headers`` and
``current_account``, so a wrong port, a renamed route or a broken auth header
shipped unnoticed. This test replaces none of them.

It starts the real backend in the desktop local profile (SQLite under a tmp
``HUGAGENT_HOME``, ``HUGAGENT_DESKTOP_BRIDGE_SECRET`` set) on an ephemeral port,
pushes a shell identity the way the desktop shell does, and runs each operation
through the real ``local_api.execute`` with headers from the real
``runner.current_account`` + ``runner._bridge_headers``, in the server's own event
loop, as the device link does in production.

The scenario runs in a child process: ``core.config.settings`` snapshots the
environment on first import, and the pytest process has already imported it with
the test profile. Running this file as a script is that child.

Hermetic: no network (the cloud base is ``*.invalid``), no MCP or script_runner
sidecars (``start_local_sidecars`` is the one thing replaced), ports chosen by the
OS, everything under a tmp dir. ``chat.send`` is not covered: it needs a model.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

RESULT_MARKER = "DEVICE_LINK_LOOPBACK_RESULT "
CLOUD = "https://cloud.test.invalid"
CLOUD_USER = "dl_loopback"
# What the shared receipt protocol (hugagent_cli.contract) guarantees for a failure.
FAILURE_FLAGS = ("not_executed", "outcome_unknown")


# ── child process ────────────────────────────────────────────────────────────────────────


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _summary(value):
    """What the parent asserts on: the result's type and keys, plus a few ids."""
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(value), "value": {k: value[k] for k in value if k in {
            "id", "chat_id", "project_id", "title", "name", "error", "not_executed", "outcome_unknown",
            "deleted", "total", "pinned",
        }}}
    if isinstance(value, list):
        return {"type": "list", "len": len(value)}
    return {"type": type(value).__name__}


def _child() -> None:
    import asyncio
    import base64
    import time

    port = _free_port()
    # Spare loopback port for the sandbox runner URL, so nothing points at a real install's 8900.
    os.environ["SANDBOX_RUNNER_URL"] = f"http://127.0.0.1:{_free_port()}"
    os.environ["WEB_CONCURRENCY"] = "1"
    import cli

    cli.apply_local_env(port)
    from hugagent_cli.schema import ensure_schema_and_seed

    ensure_schema_and_seed()

    import core.infra.local_runtime.startup as local_startup

    async def _no_sidecars():
        return None

    local_startup.start_local_sidecars = _no_sidecars

    import httpx
    import uvicorn

    from api.app import app
    from core.config.settings import settings
    from features.desktop.auth.bridge import BRIDGE_SECRET_ENV, bridge_headers
    from features.desktop.bridge.identity import shell_user_center_id
    from personal_agent.device_link import local_api, runner

    secret = os.environ[BRIDGE_SECRET_ENV]
    out: dict = {"port": port, "settings_port": settings.server.port, "ops": {}}

    async def call(name, args, headers):
        try:
            result = await asyncio.wait_for(local_api.execute(name, args, headers), 60)
        except Exception as exc:  # noqa: BLE001 — reported to the parent as data
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            out["ops"][name] = {"ok": False, "exception": type(exc).__name__, "status": status,
                                "message": str(exc)[:300]}
            return None
        out["ops"][name] = {"ok": True, **_summary(result)}
        return result

    async def scenario():
        base = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                               access_log=False, lifespan="on"))
        serving = asyncio.create_task(server.serve())
        try:
            for _ in range(600):
                if server.started or serving.done():
                    break
                await asyncio.sleep(0.1)
            if not server.started:
                raise RuntimeError("server did not start")
            async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as client:
                # The shell pushes the cloud identity with the bridge secret as bearer and no Origin.
                claims = {"c": CLOUD_USER, "d": "dev_loopback", "u": "sub_loopback", "e": time.time() + 3600}
                token = "dcap2." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".sig"
                pushed = await client.post(
                    "/v1/desktop/capability/cloud-bridge",
                    json={"cloud_base": CLOUD, "token": token, "expires_in": 600, "device_id": "dev_loopback"},
                    headers={"Authorization": "Bearer " + secret},
                )
                out["identity_push"] = pushed.status_code
                expected = shell_user_center_id(CLOUD, CLOUD_USER)
                out["expected_account"] = expected
                out["current_account"] = list(runner.current_account() or [])
                # The shell's own first request creates the account's shadow user on this computer.
                shell = types_ns(user_center_id=expected, username=CLOUD_USER, email=None, avatar_url=None)
                first = await client.get("/v1/chats", headers=bridge_headers(shell))
                out["shell_first_request"] = first.status_code
                headers = await asyncio.to_thread(runner._bridge_headers, expected)

                # A project to read back, created the way the desktop UI does.
                created = await client.post("/v1/projects", headers=headers,
                                            json={"name": "Loopback project", "description": "device link test"})
                out["project_create"] = created.status_code
                body = created.json() if created.content else {}
                data = body.get("data", body) if isinstance(body, dict) else {}
                project_id = (data or {}).get("id") or (data or {}).get("project_id")

            chat = await call("chat.create", {"title": "Loopback chat", "project_id": project_id}, headers)
            chat_id = (chat or {}).get("id") or (chat or {}).get("chat_id")
            out["chat_id"] = chat_id
            await call("chat.list", {"page": 1, "page_size": 20}, headers)
            await call("chat.search", {"q": "Loopback", "scope": "all"}, headers)
            await call("project.list", {}, headers)
            await call("run.list", {}, headers)
            if project_id:
                await call("project.get", {"project_id": project_id}, headers)
                await call("project.chats", {"project_id": project_id}, headers)
            if chat_id:
                await call("chat.get", {"chat_id": chat_id}, headers)
                await call("chat.history", {"chat_id": chat_id}, headers)
                await call("chat.update", {"chat_id": chat_id, "title": "Loopback renamed", "pinned": True}, headers)
                await call("run.active", {"chat_id": chat_id}, headers)
                await call("chat.delete", {"chat_id": chat_id}, headers)

            # Failure paths through the real Mailbox.perform, which turns them into receipts.
            out["receipts"] = await receipts(expected)
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 30)

    async def receipts(account):
        from personal_agent.device_link import store

        link = store.Link(device_id="dev_loopback", token="t", name="Loopback", cloud_base=CLOUD,
                          control_enabled=False, sessions_enabled=True)
        # The consent the desktop UI saves when the user lets Hugo use sessions on this computer.
        store.save(account, link)
        mailbox = runner.Mailbox(account, link)
        # Only the cloud's "is this operation still wanted" check is faked; it answers yes.
        await mailbox.http.aclose()
        mailbox.http = httpx.AsyncClient(base_url="https://cloud.test.invalid/check",
                                         transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
        result = {}
        cases = {
            "ok_read": ("chat.list", {}),
            "missing_read": ("chat.get", {"chat_id": "chat_does_not_exist"}),
            "missing_write": ("chat.delete", {"chat_id": "chat_does_not_exist"}),
            "unsupported": ("chat.list", {"bogus": 1}),
        }
        try:
            for case, (operation, args) in cases.items():
                op = {"id": "op_" + case, "operation": operation, "args": args, "deadline": time.time() + 60}
                try:
                    receipt = await asyncio.wait_for(mailbox.perform(op), 60)
                    result[case] = {"ok": True, **_summary(receipt)}
                except Exception as exc:  # noqa: BLE001
                    result[case] = {"ok": False, "exception": type(exc).__name__, "message": str(exc)[:300]}
        finally:
            await mailbox.http.aclose()
            mailbox.journal.close()
        return result

    def types_ns(**kwargs):
        import types

        return types.SimpleNamespace(**kwargs)

    asyncio.run(scenario())
    print(RESULT_MARKER + json.dumps(out, default=str), flush=True)


# ── tests ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def loopback():
    with tempfile.TemporaryDirectory(prefix="hugagent-dl-loopback-") as home:
        env = {k: v for k, v in os.environ.items() if k not in {
            # The pytest profile's database and deployment settings must not leak into the child.
            "DATABASE_URL", "SQLITE_FALLBACK_URL", "DEPLOY_PROFILE", "JX_EDITION", "REDIS_URL", "PORT",
            "BACKEND_PORT", "SANDBOX_PROVIDER", "SANDBOX_RUNNER_URL", "HUGAGENT_CAPS_ROOT",
            "HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS",
        }}
        env.update({
            "HUGAGENT_HOME": home,
            "HUGAGENT_DESKTOP_BRIDGE_SECRET": "loopback-secret-" + os.urandom(8).hex(),
            "HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS": "0",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        })
        backend = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(backend), env.get("PYTHONPATH", "")]))
        proc = subprocess.run([sys.executable, __file__], cwd=str(backend), env=env, capture_output=True,
                              text=True, timeout=300)
        lines = [line for line in proc.stdout.splitlines() if line.startswith(RESULT_MARKER)]
        assert proc.returncode == 0 and lines, (
            f"loopback child failed (exit {proc.returncode})\n"
            f"stdout tail:\n{proc.stdout[-4000:]}\nstderr tail:\n{proc.stderr[-6000:]}"
        )
        yield json.loads(lines[-1][len(RESULT_MARKER):])


def test_local_api_targets_the_port_the_server_listens_on(loopback):
    assert loopback["settings_port"] == loopback["port"]


def test_shell_identity_resolves_to_the_linked_account(loopback):
    assert 200 <= loopback["identity_push"] < 300
    assert loopback["current_account"] == [loopback["expected_account"], CLOUD]
    assert 200 <= loopback["shell_first_request"] < 300
    assert 200 <= loopback["project_create"] < 300


@pytest.mark.parametrize(
    "operation, keys",
    [
        ("chat.create", {"chat_id"}),
        ("chat.list", {"items"}),
        ("chat.search", {"items"}),
        ("chat.get", {"chat_id"}),
        ("chat.history", {"items"}),
        ("chat.update", {"chat_id"}),
        ("run.list", None),
        ("run.active", None),
        ("project.list", {"items"}),
        ("project.get", {"project_id"}),
        ("project.chats", {"items"}),
        ("chat.delete", None),
    ],
)
def test_operation_succeeds_over_loopback(loopback, operation, keys):
    result = loopback["ops"].get(operation)
    assert result is not None, f"{operation} did not run (an earlier step failed): {loopback}"
    assert result["ok"], f"{operation} failed: {result}"
    if keys:
        assert result["type"] == "dict" and keys <= set(result["keys"]), result


def test_writes_took_effect(loopback):
    assert loopback["chat_id"]
    created = loopback["ops"]["chat.create"]["value"]
    assert created["project_id"] == loopback["ops"]["project.get"]["value"]["project_id"]
    updated = loopback["ops"]["chat.update"]["value"]
    assert updated["title"] == "Loopback renamed" and updated["pinned"] is True


def _assert_receipt_shape(receipt):
    assert receipt["ok"], f"perform raised instead of returning a receipt: {receipt}"
    assert receipt["type"] == "dict" and "error" in receipt["keys"], receipt
    flags = [flag for flag in FAILURE_FLAGS if receipt["value"].get(flag)]
    assert len(flags) == 1, f"a failure receipt carries exactly one of {FAILURE_FLAGS}: {receipt}"


def test_mailbox_perform_succeeds_through_real_auth(loopback):
    receipt = loopback["receipts"]["ok_read"]
    assert receipt["ok"] and receipt["type"] == "dict", receipt
    assert "error" not in receipt["keys"] and "items" in receipt["keys"], receipt


# Refusals that happen before the request reaches the loopback route. A "missing" case that
# reports one of these never exercised the route, so it proves nothing about it.
REFUSED_BEFORE_ROUTE = {"permission_revoked", "account_changed", "operation_revoked"}


@pytest.mark.parametrize("case", ["missing_read", "missing_write", "unsupported"])
def test_mailbox_failure_receipts_follow_the_protocol(loopback, case):
    receipt = loopback["receipts"][case]
    _assert_receipt_shape(receipt)
    if case.startswith("missing"):
        assert not any(refusal in receipt["value"]["error"] for refusal in REFUSED_BEFORE_ROUTE), receipt


if __name__ == "__main__":
    _child()
