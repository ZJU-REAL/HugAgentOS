# Docker Compose Deployment

> Last updated: July 23, 2026 ｜ [简体中文](../../zh-CN/deployment/docker-compose.md) ｜ Back to [Deployment Guide](README.md)

> **When to use**: the **standard deployment form** for teams / production — multi-user, full features. For a personal single-machine trial, the lighter [No-Docker Quick Install](quick-install.md) is available.

All HugAgentOS services are orchestrated by a single `docker-compose.yml` at the repository root, split by profiles into always-on core services, mutually exclusive sandbox sidecars (`script_runner` / `opensandbox`), and the optional memory infrastructure (`mem0`). This guide covers the full service topology, volumes and persistence, profile usage, and the rebuild flow after code changes.


## Default sandbox in Docker CE

Docker CE defaults to OpenSandbox. No-Docker installation and desktop local mode keep local execution.
For a new deployment:

```bash
python3 scripts/configure-ce-docker.py
docker build -f docker/sandbox/Dockerfile.opensandbox -t hugagent-opensandbox-custom:latest .
docker compose build backend
docker compose up -d --build
```

The initializer generates absolute host paths, an explicit project name and a random controller key.
It refuses to overwrite an existing .env. Use --project-name and --storage to select those values.
The controller network follows the project name. CE publishes no controller host port.
The backend retains its existing management socket mount; CE has no sandbox rebuild management API.
Build the runtime image first: ordinary compose build does not build on-demand sandbox images.
Validate SDK, controller, execd, egress and runtime image together using repository versions.
This change does not upgrade upstream components.

For an existing deployment, edit its existing .env explicitly: set SANDBOX_PROVIDER=opensandbox,
COMPOSE_PROFILES=opensandbox and SANDBOX_TOOLS_ENABLED=true. Supply absolute HOST_REPO_PATH and
HOST_STORAGE_PATH, a strong random OPENSANDBOX_API_KEY and an explicit COMPOSE_PROJECT_NAME.
Preserve storage and database volumes. Existing script-runner workspaces are not migrated automatically.
Follow deployment authorization rules to stop the old runner, build the runtime, and start the controller and backend.
Verify /health, /ready, files across commands, user isolation, restart adoption and idle snapshot recovery.
Initialization and dependency failures raise errors without changing the provider.
OpenSandbox preserves session files and snapshots. Python/JS commands use separate processes and do not retain variables.


Each sandbox has an independent object-mount service. The controller does not own its FUSE connections.
Initial mounting and recovery use one lifecycle. Controller restarts preserve connections; sandbox restarts require a live authorization and mount check.
Host shared-propagation directories are unnecessary. Only the mount service needs `/dev/fuse` and mount privileges.
Existing sandboxes with FUSE host binds have immutable Docker startup settings.
Preserve their workspaces and volumes before a separate migration. Do not restart or delete them during an unplanned upgrade.

## Service topology

### Core services (always started)

| Service | Container | Image / build | Ports (host→container) | Role |
|---|---|---|---|---|
| `postgres` | hugagent-postgres | `postgres:15-alpine` | `${POSTGRES_HOST_PORT:-5432}:5432` | Primary relational DB (business data, content_blocks, usage logs) |
| `redis` | hugagent-redis | `redis:7-alpine` | `${REDIS_HOST_PORT:-6380}:6379` | Session store, streaming follower (Redis Streams), rate limiting |
| `backend` | hugagent-backend | `docker/Dockerfile` (target `production`) | `${BACKEND_HOST_PORT:-3001}:${BACKEND_PORT:-3001}` | FastAPI app; runs alembic migrations automatically at startup |
| `mcp` | hugagent-mcp | `docker/Dockerfile.mcp` | none exposed | CE starts the general MCP processes; EE also starts commercial MCP servers such as database query. The backend calls them at `http://mcp:91XX/mcp/` |
| `frontend` | hugagent-frontend | `src/frontend/Dockerfile` | `${FRONTEND_PORT:-3002}:80` | nginx serving the frontend static bundle + `/api` reverse proxy to the backend |

`BACKEND_PORT` is the container-internal listen port used by nginx, MCP, and the health check, and should normally remain `3001`. If host ports are occupied, adjust only `BACKEND_HOST_PORT`, `POSTGRES_HOST_PORT`, or `REDIS_HOST_PORT`; do not change the container-internal ports. For example, publish the backend on `13003` while keeping its internal port at `3001`.

On the first startup with an empty database, CE globally installs and enables
the `automation`, `skill-manager`, and `sites` plugins. Every user can use them
without installing them separately from the plugin marketplace. The CE
capability page, runtime catalog, MCP port registry, and MCP image don't contain
the database-query tool. That capability exists only in deployments marked as
Enterprise Edition (EE).

### Sandbox sidecars (pick one profile; mutually exclusive)

| Service | Profile | Container | Image / build | Role |
|---|---|---|---|---|
| `script-runner` | `script_runner` | hugagent-script-runner | `docker/sandbox/Dockerfile.script-runner` | Lightweight sandbox: 1 GB RAM / 1 CPU / read-only rootfs + tmpfs; skill directory mounted read-only at `/workspace/skills` |
| `opensandbox-config-init` | `opensandbox` | hugagent-opensandbox-config-init | `python:3.12-alpine` | One-shot init: renders `@@HOST_REPO_PATH@@` / `@@HOST_STORAGE_PATH@@` in `docker/opensandbox-server/opensandbox-config.toml.tpl` into the named volume `opensandbox_config` |
| `opensandbox` | `opensandbox` | hugagent-opensandbox | `hugagent-opensandbox-server:v0.1.13-3` | Shared CE / EE controller: manages containers through Docker socket; execd runs commands and session files persist. CE publishes no host port; the full repository has a configurable debug port |

### mem0 memory infrastructure (profile `mem0`, optional)

| Service | Container | Image | Ports | Role |
|---|---|---|---|---|
| `etcd` | hugagent-etcd | `quay.io/coreos/etcd:v3.5.5` | internal | Milvus metadata store |
| `minio` | hugagent-minio | `minio/minio:RELEASE.2023-03-13...` | internal | Milvus object storage |
| `milvus` | hugagent-milvus | `milvusdb/milvus:v2.5.4` | `19530`, `9091` | Vector DB (L2 vector memory, self-hosted KB retrieval) |
| `neo4j` | hugagent-neo4j | `neo4j:5.15-community` | `7474`, `7687` | Graph DB (L3 knowledge-graph memory, optional) |

### Dependency graph

```
frontend ──depends_on──► backend ──depends_on──► postgres (healthy)
                            │                    redis    (healthy)
                            │                    mcp      (started)
                            │
                            ├── (script_runner profile) ──► script-runner
                            └── (opensandbox profile)
                                  opensandbox ──depends_on──► opensandbox-config-init (completed)
mcp ──depends_on──► postgres (healthy)
milvus ──depends_on──► etcd (healthy) + minio (healthy)
```

The base Compose services join the same bridge network `hugagent-network`; containers reach each other by service name (e.g. `http://backend:3001`, `http://mcp:9102/mcp/`, `http://milvus:19530`). Every container shares a json-file log-rotation policy (50 MB × 5 = 250 MB cap per container) to keep logs from filling the disk.

## Volumes and persistence

| Volume / mount | Mounted into | Contents | Notes |
|---|---|---|---|
| `postgres_data` (named volume) | postgres `/var/lib/postgresql/data` | All business data | **Survives redeployments**; deleting it drops the database |
| `redis_data` | redis `/data` | AOF persistence | |
| `${HOST_STORAGE_PATH}` (**bind mount**) | backend, mcp `/app/storage` | File storage, myspace, sandbox_skills | Must be a host absolute path in `.env`; a bind mount (not a named volume) so OpenSandbox sandboxes can host-bind the same path |
| `manual_data` / `page_config_data` | backend + frontend | Manual / page-config static assets | Written by the backend, read by frontend nginx |
| `opensandbox_config` / `opensandbox_data` | opensandbox | Rendered config.toml / runtime data | Config generated by the init service; mounted read-only into opensandbox |
| `etcd_data` / `minio_data` / `milvus_data` / `neo4j_data` / `neo4j_logs` | mem0 services | Vector / graph data | |
| `./src/backend` (bind) | backend, mcp `/app/src/backend` | Source code | Hot source mount: for mcp changes `docker compose restart mcp` is enough; backend code is loaded by the uvicorn process, so it needs a rebuild/restart |
| `/var/run/docker.sock` (bind) | backend, opensandbox | Host docker daemon | Used by the admin "rebuild sandbox image" flow; `DOCKER_GID` must match the host docker group |

## Using profiles

Profiles are driven by `COMPOSE_PROFILES` in `.env` (or the `--profile` CLI flag):

```bash
# Full repository default: lightweight sandbox; Docker CE defaults to opensandbox
COMPOSE_PROFILES=script_runner

# Switch to the OpenSandbox persistent sandbox (also set SANDBOX_PROVIDER=opensandbox)
COMPOSE_PROFILES=opensandbox

# Add the mem0 memory infrastructure on top of the sandbox profile
COMPOSE_PROFILES=opensandbox,mem0
```

```bash
# Equivalent one-off CLI form
docker-compose --profile mem0 up -d
```

Full procedure for switching the sandbox provider (the two sidecars are mutually exclusive — never run both):

```bash
# 1. Edit .env: keep SANDBOX_PROVIDER and COMPOSE_PROFILES in sync
# 2. Stop the old stack
docker-compose down
# 3. Start the new one (builds the matching Dockerfile automatically)
docker-compose up -d --build
```

## Rebuild flow after code changes

Everything runs in containers — **after changing code you must rebuild the affected image and restart the container** for the change to take effect.

### Backend changes

```bash
docker-compose up -d --build backend
```

### MCP tool changes

The mcp container bind-mounts the source tree; a restart is enough (FastMCP does not auto-reload):

```bash
docker-compose restart mcp
```

### Frontend changes

Option A — full rebuild (slower, always correct):

```bash
docker-compose up -d --build frontend
```

Option B — build locally and hot-swap into the running container (faster; requires Node 20+):

```bash
cd src/frontend
npm run build
docker cp dist/. hugagent-frontend:/usr/share/nginx/html/
docker exec hugagent-frontend nginx -s reload
```

### Both backend and frontend changed

```bash
docker-compose up -d --build backend frontend
```

### Forced clean rebuild (dependency or Dockerfile changes)

If `requirements*.txt` or a Dockerfile changed and a cached layer kept your new code out of the image:

```bash
docker-compose build --no-cache backend frontend
docker-compose up -d backend frontend
```

> Verify the code inside the container: `docker exec hugagent-backend grep '<new code marker>' /app/src/backend/<file>`. When "my change has no effect", 90% of the time a cached layer skipped the fresh code.

> **Note:** The first build downloads optional DingTalk, Feishu, email, and
> browser-rendering tools. Each download has retries and a bounded timeout. If
> an optional source stays unavailable, the core backend and Python/bash
> sandbox still build; the build log identifies the integration that remains
> unavailable.

## Database migrations

The backend entrypoint migrates automatically; routine deployments need no manual steps:

- PostgreSQL: `alembic upgrade head` (with retries, 20 × 2 s by default, tunable via `DB_MIGRATION_RETRIES` / `DB_MIGRATION_RETRY_INTERVAL`)
- SQLite (local debugging): alembic migrations contain PostgreSQL-specific DDL, so it falls back to `Base.metadata.create_all()`

Manual operations (when developing a new migration):

```bash
# Run migrations inside the container
docker exec hugagent-backend alembic upgrade head

# Generate a new migration (locally, after editing core/db/models.py)
make migrate-new msg="describe change"
```

## Related source

| Feature | Files |
|---|---|
| Service orchestration | `docker-compose.yml` (CubeSandbox overlay: `docker/sandbox/compose.cube.yml`) |
| Backend image | `docker/Dockerfile` (multi-stage, target `production`) |
| MCP image | `docker/Dockerfile.mcp` |
| Sandbox images | `docker/sandbox/Dockerfile.script-runner`, `docker/sandbox/Dockerfile.opensandbox` |
| Frontend image + nginx proxy | `src/frontend/Dockerfile`, `src/frontend/nginx.conf`, `src/frontend/default.conf.template` |
| Backend entrypoint (auto migration) | `src/backend/scripts/backend_entrypoint.sh` |
| OpenSandbox config template | `docker/opensandbox-server/opensandbox-config.toml.tpl` |
| Migrations | `alembic.ini`, `src/backend/alembic/` |
| MCP port single source of truth | `src/backend/extensions/mcp_servers/_ports.py` |

### OpenSandbox command latency and controller image

The controller is built from `docker/opensandbox-server/Dockerfile.opensandbox-server` as
`hugagent-opensandbox-server:v0.1.13-3`. Based on a pinned upstream release,
it inspects known sandboxes by name and verifies ownership labels. Synchronous
Docker lookups in lifecycle and proxy routes run in a thread pool so other requests
remain responsive. Builds verify upstream source hashes and run regression tests;
review the patch before upgrading upstream. Offline deliveries must include this
controller image, rather than only the original `opensandbox/server` image.

The backend direct connection updates both SDK endpoint metadata and cached HTTP
base URLs for commands, files and health checks. Status polling and incremental
logs no longer return through the proxy. Unreachable direct endpoints or unsupported
SDK layouts retain the proxy. Initial rollout requires rebuilding the controller
image and restarting the backend and controller under the deployment authorization
process. Verify repeated commands, backend restart recovery and concurrent requests.

## Site data and browser deployment checks

Ordinary Compose startup neither enables application hosting nor rebuilds the on-demand OpenSandbox runtime image.
Complete the [application database upgrade](../modules/application-hosting.md) and [browser image checks](../modules/browser-automation.md) before deployment.
Set different APPLICATION_DB_PASSWORD and APPLICATION_OWNER_DB_PASSWORD values.
Configure shared Redis and an appropriate BROWSER_DNS_RESOLVER_URL.
Use application_owner for runtime data access.
Rerun application-database-init for existing volumes too.
Update backend, MCP, frontend and sandbox images.
Validate a fresh browser session.

### Bound build resources when WSL memory is scarce

Create a dedicated BuildKit builder from the WSL project directory:

    printf '[worker.oci]\n  max-parallelism = 1\n' > /tmp/hugagent-buildkit.toml
    docker buildx create --name hugagent-limited --driver docker-container \
      --driver-opt memory=3g --driver-opt memory-swap=4g \
      --driver-opt cpu-period=100000 --driver-opt cpu-quota=200000 \
      --buildkitd-config /tmp/hugagent-buildkit.toml
    BUILDX_BUILDER=hugagent-limited COMPOSE_PARALLEL_LIMIT=1 docker compose build backend
    BUILDX_BUILDER=hugagent-limited COMPOSE_PARALLEL_LIMIT=1 docker compose build mcp
    BUILDX_BUILDER=hugagent-limited COMPOSE_PARALLEL_LIMIT=1 docker compose build frontend
    docker buildx build --builder hugagent-limited --load \
      -f docker/sandbox/Dockerfile.opensandbox -t hugagent-opensandbox-custom:latest .
    docker buildx stop hugagent-limited

Reuse the builder if it already exists. Stop it after builds; its cache remains available.
Application hosting still requires the application-hosting Compose overlay.
Avoid running full builds alongside several browser test suites. Size WSL memory limits for other containers and editor processes too.

## Model gateway startup and troubleshooting (EE)

The model gateway uses the separate `litellm` service and the `gateway` profile.
Setting `MODEL_GATEWAY_ENABLED=true` alone does not start the container. Append
`gateway` to the existing `COMPOSE_PROFILES` list in `.env`, preserving other
profiles. The backend admin URL is `http://litellm:4000` on the shared Docker network.

To restore a deployment with an existing gateway database and key:

```bash
docker compose --profile gateway up -d --no-deps litellm
docker compose ps litellm
docker compose exec backend getent hosts litellm
```

PostgreSQL must already be healthy. Wait for LiteLLM startup migrations and its
healthy status, then verify usage statistics and call logs in the console.
Preserve the existing `litellm` database and `LITELLM_MASTER_KEY`; do not clear
data or rotate keys to restore connectivity.

For `Name or service not known`, check that the container exists, shares the
backend network, and matches `LITELLM_ADMIN_URL`. External DNS may return a proxy
address for a missing service name, so successful resolution alone does not
prove that the gateway container exists.

### Shared sandbox network

Sandboxes again use the controller's configured shared Docker network. Creation,
lookup, adoption, deletion and expiration do not create temporary networks or
connect/disconnect backend and controller endpoints. No Docker or Compose
upgrade is required. Discovery and cleanup remain scoped to the configured network.
Sandbox-to-sandbox reachability and upstream direct-mode host port mappings are
restored; this topology does not provide network isolation between sandboxes.

Existing sandboxes on independent networks are not migrated or deleted automatically.
Before deployment, inventory their files, mounts and snapshots, then authorize
session downtime and recreation while preserving workspaces and data volumes.
Do not force recovery by deleting networks. This cloud-controller change does not
require a desktop client release.
