"""Render the private PA controller config. Do not log credentials."""
import json, os
from pathlib import Path
storage_root = os.environ["HOST_STORAGE_PATH"].rstrip("/")
root = storage_root + "/personal-agent"
key = os.environ["PERSONAL_AGENT_OPENSANDBOX_API_KEY"]
network = os.environ["PERSONAL_AGENT_COMPUTE_NETWORK"]
if not Path(root).is_absolute() or len(key) < 32:
    raise ValueError("Set an absolute storage path and a controller key of at least 32 characters")
config = f"""[server]
host = "0.0.0.0"
port = 8080
api_key = {json.dumps(key)}
max_sandbox_timeout_seconds = 86400
[log]
level = "INFO"
[runtime]
type = "docker"
execd_image = "opensandbox/execd:v1.0.17"
[storage]
allowed_host_paths = [{json.dumps(storage_root)}]
volume_default_size = "1Gi"
[store]
type = "sqlite"
path = "/var/lib/opensandbox/opensandbox.db"
[docker]
network_mode = {json.dumps(network)}
drop_capabilities = ["AUDIT_WRITE","MKNOD","NET_ADMIN","NET_RAW","SYS_ADMIN","SYS_MODULE","SYS_PTRACE","SYS_TIME","SYS_TTY_CONFIG"]
no_new_privileges = true
pids_limit = 4096
[ingress]
mode = "direct"
"""
path = Path("/out/config.toml")
path.write_text(config)
path.chmod(0o600)
