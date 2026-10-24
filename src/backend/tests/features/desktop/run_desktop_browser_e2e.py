"""Open a real browser through a synced plugin without contacting a cloud service."""

import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[5]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix="desktop-browser-e2e-") as folder:
        home = Path(folder)
        backend, runner, mcp = [port() for _ in range(3)]
        base = f"http://127.0.0.1:{backend}"
        with sync_playwright() as playwright:
            chromium = (
                os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or playwright.chromium.executable_path
            )
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src/backend"),
            "BROWSER_E2E_HOME": folder,
            "HUGAGENT_HOME": folder,
            "HUGAGENT_CAPS_ROOT": folder,
            "BROWSER_E2E_MCP": f"http://127.0.0.1:{mcp}/mcp/",
            "BACKEND_INTERNAL_URL": base + "/api",
            "BACKEND_INTERNAL_TOKEN": secrets.token_urlsafe(32),
            "PLUGIN_RESOURCE_SECRET_KEY": secrets.token_urlsafe(48),
            "HUGAGENT_DESKTOP_BRIDGE_SECRET": secrets.token_urlsafe(32),
            "SANDBOX_RUNNER_URL": f"http://127.0.0.1:{runner}",
            "SANDBOX_RUNNER_TOKEN": secrets.token_urlsafe(32),
            "SCRIPT_RUNNER_WORKSPACE": str(home / "workspace"),
            "STORAGE_PATH": str(home / "storage"),
            "DATABASE_URL": "sqlite:///" + str(home / "test.db"),
            "DEPLOY_PROFILE": "local",
            "LOGIN_MODE": "local",
            "MCP_BIND_HOST": "127.0.0.1",
            "PYTHONNOUSERSITE": "1",
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE": chromium,
        }
        processes, logs = [], []
        try:
            for name, argv in [
                (
                    "runner",
                    [
                        "-m",
                        "uvicorn",
                        "core.sandbox.runner.server:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(runner),
                    ],
                ),
                (
                    "backend",
                    [
                        "-m",
                        "uvicorn",
                        "tests.browser_automation_server:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(backend),
                    ],
                ),
                (
                    "mcp",
                    [
                        "-m",
                        "extensions.mcp_servers.browser_runtime_mcp.server",
                        "--transport",
                        "streamable-http",
                        "--port",
                        str(mcp),
                    ],
                ),
            ]:
                log = open(home / (name + ".log"), "w+")
                logs.append(log)
                processes.append(
                    subprocess.Popen(
                        [sys.executable, *argv],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        **(
                            {"creationflags": subprocess.CREATE_NO_WINDOW}
                            if os.name == "nt"
                            else {"start_new_session": True}
                        ),
                    )
                )
            with httpx.Client(trust_env=False, timeout=120) as client:
                for _ in range(120):
                    try:
                        if client.get(base + "/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if any(p.poll() is not None for p in processes):
                        raise RuntimeError("fixture startup failed")
                    time.sleep(0.25)
                else:
                    raise RuntimeError("fixture startup timeout")

                def tool(name, args):
                    r = client.post(base + "/test/synced-tool/" + name, json=args)
                    r.raise_for_status()
                    return r.json()

                opened = tool("browser_open", {})
                resource = opened["resource"]
                assert resource["execution_scope"] == "local"
                rid = resource["resource_id"]
                tool(
                    "browser_action",
                    {
                        "resource_id": rid,
                        "action": "navigate",
                        "params": {"url": base + "/test/site"},
                    },
                )
                observed = tool("browser_observe", {"resource_id": rid, "action": "snapshot"})
                assert "姓名" in observed["snapshot"], observed
                assert tool("browser_open", {"_chat_id": "nonexistent-local-conversation"}) == {
                    "error": "conversation_unavailable"
                }
                tool("browser_close", {"resource_id": rid})
                print(
                    json.dumps(
                        {
                            "opened": True,
                            "scope": "local",
                            "binding": "local user + local conversation + exact synced installation",
                            "real_page_observed": True,
                            "missing_conversation_rejected": True,
                            "cloud_requests": 0,
                        },
                        ensure_ascii=False,
                    )
                )
        except BaseException:
            for log in logs:
                log.flush()
                log.seek(0)
                print(log.read()[-7000:])
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
            for log in logs:
                log.close()


if __name__ == "__main__":
    main()
