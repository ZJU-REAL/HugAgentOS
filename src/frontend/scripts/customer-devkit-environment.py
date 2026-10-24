"""No-source customer consumer and disposable local-password backend. Never reads .env."""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import time
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
PREFIX = "hugagent-devkit-e2e"
LABEL = "com.hugagent.devkit-e2e"
NAMES = [PREFIX + "-" + suffix for suffix in ["consumer", "backend", "model", "redis"]]
NETWORK = PREFIX
WORK = ROOT / ".scratch/customer-devkit-test"

def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=check)
    return result.stdout.strip() if result.returncode == 0 else ""

def down():
    for name in NAMES:
        result = docker("inspect", name, check=False)
        if not result:
            continue
        assert json.loads(result)[0]["Config"].get("Labels", {}).get(LABEL) == "true"
        docker("rm", "-f", name)
    result = docker("network", "inspect", NETWORK, check=False)
    if result:
        assert json.loads(result)[0].get("Labels", {}).get(LABEL) == "true"
        docker("network", "rm", NETWORK)
    print("Removed labelled devkit test resources")

def up():
    assert not any(docker("inspect", n, check=False) for n in NAMES), "Existing fixture resources"
    WORK.mkdir(parents=True, exist_ok=True)
    consumer = Path(tempfile.mkdtemp(prefix="customer-", dir=WORK))
    (WORK / "consumer.json").write_text(json.dumps({"path": str(consumer)}))
    subprocess.run(["tar", "-xzf", str(ROOT / ".scratch/customer-devkit/hugagent-customer-devkit-1.0.0.tar.gz"), "-C", str(consumer)], check=True)
    docker("network", "create", "--label", LABEL + "=true", NETWORK)
    common = ["run", "-d", "--label", LABEL + "=true", "--network", NETWORK]
    docker(*common, "--name", NAMES[3], "--network-alias", "redis", "redis:7-alpine")
    docker(*common, "--name", NAMES[2], "--network-alias", "model", "--no-healthcheck",
           "--entrypoint", "python", "-v", str(ROOT / "src/frontend/scripts/chat-customization-model.py") + ":/tmp/model.py:ro", "hugagent-backend", "/tmp/model.py")
    env = {"DATABASE_URL": "sqlite:////app/storage/devkit.db", "AUTH_MODE": "session",
           "LOCAL_AUTH_ENABLED": "true", "SSO_MOCK_ENABLED": "false", "SSO_LOGIN_MODE": "local",
           "SSO_EXCHANGE_MODE": "local", "OA_SSO_ENABLED": "false", "SESSION_STORE": "memory",
           "SESSION_COOKIE_SECURE": "false", "REDIS_URL": "redis://redis:6379/0",
           "JX_EDITION": "ee", "JX_LICENSE_REQUIRED": "false", "MEM0_ENABLED": "false",
           "ENABLE_SUMMARY": "false", "SANDBOX_TOOLS_ENABLED": "false", "STORAGE_TYPE": "local",
           "STORAGE_PATH": "/app/storage", "SSO_REDIRECT_ORIGIN_ALLOWLIST": "http://127.0.0.1:4310,http://localhost:4310,http://127.0.0.1:4312,http://localhost:4312"}
    args = [*common, "--name", NAMES[1], "--network-alias", "backend", "-p", "127.0.0.1:3411:3001",
            "--tmpfs", "/app/storage:mode=1777", "-v", str(ROOT / "src/backend") + ":/app/src/backend:ro"]
    for key, value in env.items():
        args.extend(["-e", key + "=" + value])
    docker(*args, "hugagent-backend")
    for _ in range(90):
        try:
            with urllib.request.urlopen("http://127.0.0.1:3411/ready", timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("Devkit backend not ready")
    seed = """from core.db.engine import SessionLocal
from core.models.repository import create_provider, assign_role
from core.auth.local_users import LocalUserService
with SessionLocal() as db:
    result = LocalUserService(db).create_by_admin('devkit_user', 'Devkit-Test-Only-456', nickname='Devkit Tester')
    assert result.ok, result.message
    provider = create_provider(db, display_name='Devkit E2E', provider_type='chat', base_url='http://model:8081/v1', api_key='test-placeholder', model_name='devkit-test', extra_config={'api_protocol':'chat_completions','supports_thinking':True,'context_length':32000})
    assert assign_role(db, 'main_agent', provider.provider_id, updated_by='devkit-e2e')
"""
    subprocess.run(["docker", "exec", "-i", NAMES[1], "python", "-"], input=seed, text=True, check=True)
    docker(*common, "--name", NAMES[0], "-p", "127.0.0.1:4310:4310", "-p", "127.0.0.1:4312:4312",
           "-v", str(consumer) + ":/customer", "-w", "/customer",
           "-e", "HUGAGENT_BACKEND_URL=http://backend:3001", "-e", "HUGAGENT_DEV_HOST=0.0.0.0",
           "node:22-alpine", "sh", "-c",
           "npm ci --no-audit --no-fund && npm run build && npm run build:web && npm pack && npm run dev:headless")
    print("Customer consumer starting on http://127.0.0.1:4310; only the exported kit is mounted")

if __name__ == "__main__":
    if sys.argv[1:] == ["up"]:
        up()
    elif sys.argv[1:] == ["down"]:
        down()
    else:
        raise SystemExit("Usage: customer-devkit-environment.py up|down")
