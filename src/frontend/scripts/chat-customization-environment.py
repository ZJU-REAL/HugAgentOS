"""Disposable customer-chat full-app test environment. Run from WSL project root; never uses .env."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
NETWORK = "hugagent-customization-e2e"
LABEL = "com.hugagent.customization-e2e"
NAMES = ["hugagent-customization-e2e-frontend", "hugagent-customization-e2e-backend", "hugagent-customization-e2e-model", "hugagent-customization-e2e-redis"]
CONF = 'server { listen 80; root /usr/share/nginx/html; index index.html; location / { try_files $uri $uri/ /index.html; } location /mock-sso/ { proxy_pass http://customization-backend:3001/mock-sso/; } location /login { proxy_pass http://customization-backend:3001; } location /api/ { proxy_pass http://customization-backend:3001/; proxy_buffering off; proxy_read_timeout 180s; client_max_body_size 50m; } }'

def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=check)
    return result.stdout.strip()

def exists(name):
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0

def down():
    for name in NAMES:
        if not exists(name):
            continue
        metadata = json.loads(docker("inspect", name))[0]
        if metadata["Config"].get("Labels", {}).get(LABEL) != "true":
            raise RuntimeError("Refusing to remove an unlabelled container: " + name)
        docker("rm", "-f", name)
    if subprocess.run(["docker", "network", "inspect", NETWORK], capture_output=True).returncode == 0:
        metadata = json.loads(docker("network", "inspect", NETWORK))[0]
        if metadata.get("Labels", {}).get(LABEL) != "true":
            raise RuntimeError("Refusing to remove an unlabelled network")
        docker("network", "rm", NETWORK)
    print("Removed only the labelled disposable test environment.")

def up():
    if any(exists(name) for name in NAMES):
        raise RuntimeError("Test containers already exist; run down first.")
    work = ROOT / ".scratch" / "chat-customization-e2e"
    work.mkdir(parents=True, exist_ok=True)
    (work / "nginx.conf").write_text(CONF)
    public = work / "public"
    source = Path(os.environ.get("CHAT_E2E_DIST", str(ROOT / "src/frontend/dist"))).resolve()
    shutil.copytree(source, public, dirs_exist_ok=True)
    shutil.copytree(ROOT / "examples/customer-chat/standalone-dist", public / "customer", dirs_exist_ok=True)
    shutil.copytree(ROOT / "packages/chat-sdk/dist", public / "sdk", dirs_exist_ok=True)
    if not (ROOT / "src/frontend/dist/index.html").exists():
        raise RuntimeError("Build the frontend before starting the test environment.")
    docker("network", "create", "--label", LABEL + "=true", NETWORK)
    common = ["run", "-d", "--label", LABEL + "=true", "--network", NETWORK]
    docker(*common, "--name", NAMES[3], "--network-alias", "redis", "redis:7-alpine")
    docker(*common, "--no-healthcheck", "--name", NAMES[2], "--network-alias", "model", "--entrypoint", "python", "-v", str(ROOT / "src/frontend/scripts/chat-customization-model.py") + ":/tmp/model.py:ro", "hugagent-backend", "/tmp/model.py")
    env = {"DATABASE_URL": "sqlite:////app/storage/e2e.db", "AUTH_MODE": "session", "LOCAL_AUTH_ENABLED": "false", "SSO_MOCK_ENABLED": "true", "SSO_LOGIN_MODE": "mock", "SSO_EXCHANGE_MODE": "mock", "MOCK_SSO_APP_BASE": "http://localhost:3400", "SESSION_STORE": "memory", "SESSION_COOKIE_SECURE": "false", "REDIS_URL": "redis://redis:6379/0", "JX_EDITION": "ee", "JX_LICENSE_REQUIRED": "false", "MEM0_ENABLED": "false", "ENABLE_SUMMARY": "false", "SANDBOX_TOOLS_ENABLED": "false", "STORAGE_TYPE": "local", "STORAGE_PATH": "/app/storage"}
    args = [*common, "--name", NAMES[1], "--network-alias", "customization-backend", "-p", "127.0.0.1:3401:3001", "--tmpfs", "/app/storage:mode=1777", "-v", str(ROOT / "src/backend") + ":/app/src/backend:ro"]
    for key, value in env.items():
        args.extend(["-e", key + "=" + value])
    docker(*args, "hugagent-backend")
    for _ in range(90):
        try:
            with urllib.request.urlopen("http://127.0.0.1:3401/ready", timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("Isolated backend did not become ready; inspect its logs.")
    seed = """from core.db.engine import SessionLocal
from core.models.repository import create_provider, assign_role
with SessionLocal() as db:
    provider = create_provider(db, display_name='Component E2E', provider_type='chat', base_url='http://model:8081/v1', api_key='e2e-placeholder', model_name='component-test', extra_config={'api_protocol':'chat_completions','supports_thinking':True,'context_length':32000})
    assert assign_role(db, 'main_agent', provider.provider_id, updated_by='component-e2e')
"""
    subprocess.run(["docker", "exec", "-i", NAMES[1], "python", "-"], input=seed, text=True, check=True)
    docker(*common, "--name", NAMES[0], "-p", "127.0.0.1:3400:80", "--entrypoint", "nginx", "-v", str(public) + ":/usr/share/nginx/html:ro", "-v", str(work / "nginx.conf") + ":/etc/nginx/conf.d/default.conf:ro", "hugagent-frontend", "-g", "daemon off;")
    print("Isolated full application ready at http://localhost:3400; model replies are deterministic.")

if __name__ == "__main__":
    if sys.argv[1:] == ["up"]:
        up()
    elif sys.argv[1:] == ["down"]:
        down()
    else:
        raise SystemExit("Usage: component-e2e-environment.py up|down")
