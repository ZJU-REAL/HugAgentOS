"""Environment-backed configuration values."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from .environment import _env, _bool, _int

@dataclass(frozen=True)
class SandboxSettings:
    """Sandbox provider configuration.

    ``SANDBOX_PROVIDER`` switches the underlying execution environment:
    - ``script_runner`` (default): built-in Docker container + setrlimit subprocess
    - ``opensandbox``: Alibaba OpenSandbox (Docker container + persistent Jupyter context)
    - ``cube``: Tencent CubeSandbox (external E2B-compatible MicroVM node)
    """

    provider: str = field(
        default_factory=lambda: _env("SANDBOX_PROVIDER", "script_runner").strip().lower()
    )

    # script_runner sidecar call parameters. Defaults to the compose service name (not the container name), decoupled from container renames.
    runner_url: str = field(
        default_factory=lambda: _env("SANDBOX_RUNNER_URL", "http://script-runner:8900")
    )
    enabled: bool = field(default_factory=lambda: _bool(_env("SANDBOX_TOOLS_ENABLED", "false")))
    file_transfer_timeout_s: int = field(
        default_factory=lambda: _int(_env("SANDBOX_FILE_TRANSFER_TIMEOUT_S", "120"), 120)
    )
    # The one lifetime parameter for sandboxes (seconds), shared by every
    # provider: server-side TTL, the idle snapshot-then-release threshold and the
    # keepalive interval are all derived from it. It used to be four separate
    # knobs (opensandbox/cube x TTL/idle-reap) that could disagree, letting the
    # shortest one reclaim a chat sandbox before the others had snapshotted it.
    idle_ttl_s: int = field(
        default_factory=lambda: max(60, _int(_env("SANDBOX_IDLE_TTL_S", "3600"), 3600))
    )
    artifact_max_bytes: int = field(
        default_factory=lambda: max(
            1,
            _int(
                _env("SANDBOX_ARTIFACT_MAX_BYTES", str(100 * 1024 * 1024)),
                100 * 1024 * 1024,
            ),
        )
    )

    # opensandbox
    opensandbox_domain: str = field(
        default_factory=lambda: _env("OPENSANDBOX_DOMAIN", "http://opensandbox:8080")
    )
    opensandbox_api_key: str = field(default_factory=lambda: _env("OPENSANDBOX_API_KEY", ""))
    opensandbox_image: str = field(
        default_factory=lambda: _env("OPENSANDBOX_IMAGE", "opensandbox/code-interpreter:v1.0.2")
    )
    opensandbox_ready_timeout_s: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_READY_TIMEOUT_S", "90"), 90)
    )
    opensandbox_request_timeout_s: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_REQUEST_TIMEOUT_S", "120"), 120)
    )
    # Direct execd connection: bypasses the OpenSandbox server's proxy
    # forwarding (measured ~3s extra buffering overhead per request — file
    # read/write 3s→<5ms, commands 4s→1s). Connects directly to the sandbox
    # container sandbox-<id>:44772; requires backend and sandbox containers to
    # be reachable on the same docker network (true for this project's compose
    # topology). A one-time reachability probe runs at creation; if
    # unreachable, it auto-falls back to the server proxy with zero feature loss.
    opensandbox_direct_execd_enabled: bool = field(
        default_factory=lambda: _bool(_env("OPENSANDBOX_DIRECT_EXECD", "true"))
    )
    # endpoint fastpath: skips the server's GET /endpoints/{port} (measured
    # fixed ~3.3s hard server-side latency; Sandbox.create calls it once each
    # for execd+egress → saves ~6.6s cold start). The returned proxy endpoint
    # is a deterministic string constructible locally. Enabled only in insecure
    # mode (empty api_key): with an api_key the server may stuff auth/routing
    # headers into the endpoint that cannot be replicated locally.
    opensandbox_endpoint_fastpath_enabled: bool = field(
        default_factory=lambda: _bool(_env("OPENSANDBOX_ENDPOINT_FASTPATH", "true"))
    )
    # Warm pool: fill each bucket to min_idle right after process startup so the first user gets a warm sandbox
    opensandbox_pool_jupyter_min_idle: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_POOL_JUPYTER_MIN_IDLE", "2"), 2)
    )
    opensandbox_pool_jupyter_max_idle: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_POOL_JUPYTER_MAX_IDLE", "3"), 3)
    )
    opensandbox_pool_light_min_idle: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_POOL_LIGHT_MIN_IDLE", "2"), 2)
    )
    opensandbox_pool_light_max_idle: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_POOL_LIGHT_MAX_IDLE", "5"), 5)
    )
    # Deployment-wide sandbox capacity, enforced by the backend admission ledger
    # (core/sandbox/providers/opensandbox/admission.py) across all workers.
    opensandbox_pool_max_total: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_POOL_MAX_TOTAL", "20"), 20)
    )
    # Concurrent controller creations, protecting the Docker daemon.
    opensandbox_create_concurrency: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_CREATE_CONCURRENCY", "4"), 4)
    )
    # How long an interactive creation waits for a free slot before reporting
    # that capacity is exhausted. Pre-warm never waits.
    opensandbox_admission_wait_s: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_ADMISSION_WAIT_S", "30"), 30)
    )
    # Slots pre-warm may never take, so a warm pool cannot crowd out users.
    opensandbox_user_headroom: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_USER_HEADROOM", "2"), 2)
    )
    # Liveness probe (GET /sandboxes/{id}) timeout (seconds) before taking an
    # idle sandbox out of the pool. Unreachable/timeout does not delete it —
    # left for acquire / the next round to handle. See
    # _OpenSandboxSessionMixin._probe_pooled_sandbox_alive.
    opensandbox_pool_liveness_probe_timeout_s: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_POOL_LIVENESS_PROBE_TIMEOUT_S", "5"), 5)
    )
    # ─── Snapshot persistence (see internal design docs) ─────────────
    # Master switch: true → enable snapshot park/restore + background worker;
    # false → fall back to the status quo (idle sandboxes are lost when reaped;
    # on reconnect, bash returns 404 if the sandbox is already dead).
    opensandbox_snapshot_enabled: bool = field(
        default_factory=lambda: _bool(_env("OPENSANDBOX_SNAPSHOT_ENABLED", "true"))
    )
    # Snapshot retention in the DB (days); expired ones are deleted by the GC worker (DB row + opensandbox side).
    opensandbox_snapshot_retention_days: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_SNAPSHOT_RETENTION_DAYS", "7"), 7)
    )
    # Max polling time (seconds) waiting for snapshot accept→Ready. Measured ~60s; 120s gives a 1× safety margin.
    opensandbox_snapshot_wait_timeout_s: int = field(
        default_factory=lambda: _int(_env("OPENSANDBOX_SNAPSHOT_WAIT_TIMEOUT_S", "120"), 120)
    )
    # Skill dependencies come only from the image bake (docker/sandbox/Dockerfile.opensandbox); new dependencies go through the admin-panel rebuild.

    # The all-modes code-execution capability switch has moved out of settings:
    # the sole control source is sandbox.code_capability_enable in the Config
    # admin panel "System Config" (option B, see
    # core.config.store.system_config.code_capability_enabled). The env is retired.
    # Hard safety for user confirmation of MySpace write operations (docs §13).
    # true (default): Write/Edit/Delete/Move on /myspace must pass out-of-band
    # user confirmation; non-interactive mode (sub-agent) rejects writes
    # outright. Ops can set false to disable (trusted-path policy soft
    # constraint). With code_capability enabled this is the key protection
    # against the main agent accidentally modifying the user's private drive.
    myspace_write_confirm: bool = field(
        default_factory=lambda: _bool(_env("MYSPACE_WRITE_CONFIRM", "true"))
    )

    # User confirmation for automation (cron task) changes (borrows the §13
    # MySpace write-confirm suspend gate). true (default): when the Agent
    # creates/edits/deletes cron tasks via the automation plugin
    # (automation_task MCP), out-of-band user confirmation is required in web
    # interactive chats; channel (IM bot) runs and non-interactive modes
    # (sub-agent/plan execution/scheduler-triggered) skip the prompt and
    # pass directly. Set false to disable.
    automation_write_confirm: bool = field(
        default_factory=lambda: _bool(_env("AUTOMATION_WRITE_CONFIRM", "true"))
    )

    # Host-visible storage root used for local file volumes.
    opensandbox_host_storage_path: str = field(
        default_factory=lambda: _env("HOST_STORAGE_PATH", "").strip(),
    )

    # ─── dws (DingTalk Workspace CLI) integration ─────────────────────────
    # The dingtalk marketplace skill runs dws inside the sandbox under the
    # current user's DingTalk identity. Credentials (per-user OAuth) live on a
    # per-user bind-mount persistent volume (~/.dws + ~/.local/share/dws-cli),
    # surviving across sessions. This is an **architecture switch** (part of
    # the sandbox mechanism), so it stays in settings; the Custom App's
    # client-id/secret/trusted_domains are **operational config**, stored via
    # the Config admin platform "System Config → DingTalk Workspace" DB config
    # (dingtalk.* system-config keys), not in .env/compose. See
    # internal design docs.
    dws_creds_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("DWS_CREDS_BIND_MOUNT_ENABLED", "true")),
    )

    # ─── lark-cli (Feishu/Lark CLI) integration ──────────────────────────
    # The feishu-cli marketplace plugin's lark-* skills run lark-cli inside the
    # sandbox under the current user's Feishu identity. Credentials (per-user
    # OAuth, file-based encryption) live on a per-user bind-mount persistent
    # volume (~/.lark-cli + ~/.local/share/lark-cli, including master.key),
    # surviving across sessions. Architecture switch, stays in settings; the
    # app's app_id/app_secret are operational config via "System Config →
    # Feishu Workspace" DB config (lark.* keys). See
    # internal design docs.
    lark_creds_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("LARK_CREDS_BIND_MOUNT_ENABLED", "true")),
    )

    # ─── himalaya (email CLI) integration ────────────────────────────────
    # The email marketplace plugin's email skill runs himalaya inside the
    # sandbox to send/manage mail under the current user's mailbox identity.
    # Credentials (per-user IMAP/SMTP app passwords, plain file-based
    # config.toml) live on a per-user bind-mount persistent volume
    # (~/.config/himalaya), surviving across sessions. Architecture switch,
    # stays in settings; the mailbox address/app password is **user-provided**
    # (not deployment-level), written by the backend into the per-user
    # config.toml — not in system config, not in .env. See
    # internal design docs.
    email_creds_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("EMAIL_CREDS_BIND_MOUNT_ENABLED", "true")),
    )

    # ─── openyida (Yida low-code platform CLI) integration ──────────────
    # The yida marketplace plugin's skills use the openyida CLI inside the
    # sandbox to operate Yida (apps/forms/pages/workflows/reports etc.). The
    # login state is a plain file cookie (<workdir>/.cache/cookies-*.json,
    # written by QR-code login); a per-user bind-mount persistent volume mounts
    # the fixed working directory ~/yida-workspace into the sandbox, surviving
    # across sessions. Unlike dws/lark/email: login completes inside the chat
    # (openyida login --agent-qr shows a QR code, user scans with DingTalk) —
    # no backend subprocess login, no DB connection table, no system-config keys.
    yida_creds_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("YIDA_CREDS_BIND_MOUNT_ENABLED", "true")),
    )

    # cube (Tencent CubeSandbox, E2B-compatible MicroVM; external node, no local sidecar needed)
    cube_api_url: str = field(
        default_factory=lambda: _env("CUBE_API_URL", "http://cube-node:38473")
    )
    cube_api_key: str = field(default_factory=lambda: _env("CUBE_API_KEY", ""))
    # Data-plane sandbox domain, may include a port (when cube-proxy is not on 443); the SDK builds https://{port}-{id}.{domain} from it
    cube_api_sandbox_domain: str = field(
        default_factory=lambda: _env("CUBE_API_SANDBOX_DOMAIN", "cube.app:38573")
    )
    # Required: sandbox template id (CubeSandbox requires it when creating a sandbox)
    cube_template: str = field(default_factory=lambda: _env("CUBE_TEMPLATE", "").strip())
    cube_request_timeout_s: int = field(
        default_factory=lambda: _int(_env("CUBE_REQUEST_TIMEOUT_S", "120"), 120)
    )
    # mkcert rootCA bundle (in-container path); when non-empty, injects SSL_CERT_FILE so the SDK trusts the self-signed cert
    cube_ca_bundle: str = field(default_factory=lambda: _env("CUBE_CA_BUNDLE", "").strip())
    # Warm-pool target idle count: refilled in the background to this value on
    # startup / after each take, so a new session's first run gets a warm
    # sandbox, skipping AsyncSandbox.create's MicroVM cold start (~10s). <=0
    # disables the warm pool.
    cube_pool_min_idle: int = field(
        default_factory=lambda: _int(_env("CUBE_POOL_MIN_IDLE", "2"), 2)
    )
    # Sandbox owner tag: written into metadata["hugagent-owner"], used so the
    # startup orphan sweep only recognizes this environment's sandboxes.
    # CubeMaster (MVP) does not honor the sandbox TTL; a backend restart loses
    # session/pool in-memory state and orphaned sandboxes linger on the node.
    # At startup, sandboxes of this environment are listed by this tag and
    # orphans not in the registry are cleaned. When multiple environments share
    # the same cube node, **each must set a unique value** (e.g. hugagent-dev /
    # hugagent-test / hugagent-test), otherwise the sweep would kill other
    # environments' sandboxes. Empty: metadata still writes "backend", but the
    # startup orphan sweep is **disabled** (the safe default on shared nodes).
    cube_owner_tag: str = field(default_factory=lambda: _env("CUBE_OWNER_TAG", "").strip())

    # ─── Skill on-demand push optimization (bundled transfer + session-bind pre-push) ─────
    # On-demand skill pushes (built-in + DB) switch to "tar bundle → single
    # upload → extract inside the sandbox", avoiding pushing tens of thousands
    # of small files one by one. Pre-push: when a session first binds a warm
    # sandbox, enabled skills are pre-pushed in the background, overlapping the
    # first LLM turn to hide latency; only skills whose tar is ≤ max_mb are
    # pre-pushed (oversized skills like ppt-master remain on-demand, paying one
    # upload only when actually used — avoids wasting tens of MB of pushes on
    # text-only sessions).
    cube_skill_prepush: bool = field(
        default_factory=lambda: _bool(_env("CUBE_SKILL_PREPUSH", "true"))
    )
    cube_skill_prepush_max_mb: int = field(
        default_factory=lambda: _int(_env("CUBE_SKILL_PREPUSH_MAX_MB", "20"), 20)
    )
    cube_skill_prepush_concurrency: int = field(
        default_factory=lambda: _int(_env("CUBE_SKILL_PREPUSH_CONCURRENCY", "3"), 3)
    )

    # ─── Cube remote template rebuild (the admin "Sandbox deps → App deps" path when provider=cube) ──
    # The backend does not build locally; instead it scps the aggregated
    # dependency manifest to the cube node + triggers via ssh a docker build on
    # the node → push to the local registry → cubemastercli create-from-image,
    # then hot-swaps _template with the new template id + writes it back to
    # .env. An empty host means "remote rebuild not configured": the rebuild
    # endpoint errors out for cube and directs users to the manual docs path.
    # See features/sandbox_ops/cube_template.py for details.
    cube_node_ssh_host: str = field(
        default_factory=lambda: _env("CUBE_NODE_SSH_HOST", _env("CUBE_NODE_IP", "")).strip()
    )
    cube_node_ssh_port: int = field(
        default_factory=lambda: _int(_env("CUBE_NODE_SSH_PORT", "22"), 22)
    )
    cube_node_ssh_user: str = field(
        default_factory=lambda: _env("CUBE_NODE_SSH_USER", "root").strip()
    )
    # In-container private key path (docker-compose mounts the host key read-only); empty uses the default ssh key chain
    cube_node_ssh_key: str = field(default_factory=lambda: _env("CUBE_NODE_SSH_KEY", "").strip())
    # Build context directory on the node (Dockerfile.cube-sandbox and dependency manifests are scp'd flat into it; the src tree is rsync'd on first deployment)
    cube_build_ctx_dir: str = field(
        default_factory=lambda: _env("CUBE_BUILD_CTX_DIR", "/opt/cube-build").strip()
    )
    cube_build_image_tag: str = field(
        default_factory=lambda: _env("CUBE_BUILD_IMAGE_TAG", "hugagent-cube-sandbox:latest").strip()
    )
    cube_build_registry: str = field(
        default_factory=lambda: _env("CUBE_BUILD_REGISTRY", "127.0.0.1:5000").strip()
    )
    # create-from-image resource/port parameters (aligned with the existing READY template)
    cube_build_writable_layer: str = field(
        default_factory=lambda: _env("CUBE_BUILD_WRITABLE_LAYER", "8Gi").strip()
    )
    cube_build_cpu: int = field(default_factory=lambda: _int(_env("CUBE_BUILD_CPU", "2000"), 2000))
    cube_build_memory: int = field(
        default_factory=lambda: _int(_env("CUBE_BUILD_MEMORY", "4000"), 4000)
    )
    # Comma-separated exposed ports; probe port + path
    cube_build_expose_ports: str = field(
        default_factory=lambda: _env("CUBE_BUILD_EXPOSE_PORTS", "49983,49999").strip()
    )
    cube_build_probe_port: int = field(
        default_factory=lambda: _int(_env("CUBE_BUILD_PROBE_PORT", "49999"), 49999)
    )
    cube_build_probe_path: str = field(
        default_factory=lambda: _env("CUBE_BUILD_PROBE_PATH", "/health").strip()
    )
    # Separate timeout ceilings for build / registration (seconds)
    cube_build_timeout_s: int = field(
        default_factory=lambda: _int(_env("CUBE_BUILD_TIMEOUT_S", "1800"), 1800)
    )
    cube_build_register_timeout_s: int = field(
        default_factory=lambda: _int(_env("CUBE_BUILD_REGISTER_TIMEOUT_S", "900"), 900)
    )
