"""Run isolated subprocesses and actual Canvas/Chromium E2E, clean all on exit."""
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[3]
def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def main():
    with tempfile.TemporaryDirectory(prefix="browser-e2e-") as folder:
        home = Path(folder)
        backend, runner, mcp = [port() for _ in range(3)]
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src/backend"),
            "BROWSER_E2E_HOME": folder,
            "BROWSER_E2E_ASSETS": str(ROOT / "src/frontend/node_modules/.tmp/browser-automation"),
            "BROWSER_E2E_BACKEND": f"http://127.0.0.1:{backend}",
            "BROWSER_E2E_MCP": f"http://127.0.0.1:{mcp}/mcp",
            "BACKEND_INTERNAL_URL": f"http://127.0.0.1:{backend}/api",
            "BACKEND_INTERNAL_TOKEN": secrets.token_urlsafe(32),
            "HUGAGENT_DESKTOP_BRIDGE_SECRET": secrets.token_urlsafe(32),
            "PLUGIN_RESOURCE_SECRET_KEY": secrets.token_urlsafe(48),
            "SANDBOX_RUNNER_URL": f"http://127.0.0.1:{runner}",
            "SANDBOX_RUNNER_TOKEN": secrets.token_urlsafe(32),
            "SCRIPT_RUNNER_WORKSPACE": str(home / "workspace"),
            "DEPLOY_PROFILE": "local", "LOGIN_MODE": "local",
            "DATABASE_URL": "sqlite:///" + str(home / "test.db"),
            "HUGAGENT_HOME": folder, "STORAGE_PATH": str(home / "storage"),
            "MCP_BIND_HOST": "127.0.0.1"}
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            env["CHROMIUM_EXECUTABLE"] = playwright.chromium.executable_path
            env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"] = playwright.chromium.executable_path
        env.pop("HUGAGENT_CAPS_ROOT", None)
        node = os.getenv("BROWSER_E2E_NODE") or shutil.which("node")
        if not node:
            raise RuntimeError("Node.js is required to build the real Canvas fixture")
        subprocess.run([str(node), "scripts/test-browser-automation.mjs", "--prepare"], cwd=ROOT / "src/frontend", env=env, check=True)
        processes = []
        logs = []
        try:
            for name, args in [
                ("runner", ["-m", "uvicorn", "services.script_runner_service.server:app", "--host", "127.0.0.1", "--port", str(runner)]),
                ("backend", ["-m", "uvicorn", "tests.browser_automation_server:app", "--host", "127.0.0.1", "--port", str(backend)]),
                ("mcp", ["-m", "mcp_servers.browser_runtime_mcp.server", "--transport", "streamable-http", "--port", str(mcp)])]:
                log = open(home / (name + ".log"), "w+")
                logs.append(log)
                processes.append(subprocess.Popen([sys.executable, *args], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
            proxy_log = open(home / "proxy.log", "w+")
            logs.append(proxy_log)
            processes.append(subprocess.Popen([str(node), str(ROOT / "desktop-uos/test/browser-resource-fixture.mjs")], env=env, stdout=proxy_log, stderr=subprocess.STDOUT, start_new_session=True))
            with httpx.Client(trust_env=False) as client:
                for attempt in range(60):
                    try:
                        if client.get(env["BROWSER_E2E_BACKEND"] + "/health").status_code == 200 and (home / "proxy.json").exists():
                            break
                    except httpx.HTTPError:
                        pass
                    if any(p.poll() is not None for p in processes):
                        raise RuntimeError("fixture startup failed")
                    time.sleep(.5)
                else:
                    raise RuntimeError("fixture timeout")
            import json
            env["BROWSER_E2E_FRONTEND"] = json.loads((home / "proxy.json").read_text())["origin"]
            subprocess.run([str(node), "scripts/test-browser-automation.mjs"], cwd=ROOT / "src/frontend", env=env, check=True)
        except BaseException:
            for log in logs:
                log.flush(); log.seek(0)
                content=log.read()
                relevant=[line for line in content.splitlines() if any(word in line for word in ("ERROR", "stream", "WebSocket", "resource", "Exception", "Traceback", "startup failed"))]
                print("\n".join(relevant)[-5000:] if relevant else content[-3000:])
            raise
        finally:
            import signal
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
            for log in logs:
                log.close()

if __name__ == "__main__":
    main()
