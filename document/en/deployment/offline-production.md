# Offline Production Deployment (Enterprise Edition)

> Last updated: 2026-07-16 ｜ [简体中文](../../zh-CN/deployment/offline-production.md) ｜ Back to [Deployment Guide](README.md)

> **When to use**: **air-gapped environments** (government intranets and the like) that cannot pull images online — image tarball offline delivery. Part of the Enterprise Edition service scope. For connected environments, use [Docker Compose](docker-compose.md).

Air-gapped environments (government intranets and the like) cannot pull images online, so HugAgentOS ships an offline delivery flow: build image tarballs on a connected machine → copy them across → `docker load` + `compose up` on the production side. Offline deployment and professional implementation are part of the Enterprise Edition service scope (see [Edition Comparison](../editions/overview.md)). This document follows the actual scripts under `docker/`.

## Flow overview

```
Connected build machine (test branch)            Air-gapped production machine
─────────────────────────────────────            ─────────────────────────────
scripts/deploy/deploy_prepare.sh
  ├─ git fetch + merge origin/main → test
  ├─ auto-selects the package tier by diff
  │    infra/deps changed → save_pack.sh (full pack)
  │    app code only      → save_pack_app.sh (app pack)
  └─ outputs docker/hugagent-*-<sha>-<ts>.tar.gz
            + matching .manifest.txt
                    │
                scp copy               ┌─ offline_deployment.sh (full)
                    ├─────────────────►│    docker load + compose up -d
                    │                  │    --no-build --force-recreate
(optional) prompt snapshot             │    --remove-orphans
prompts_snapshot_<ts>.json ───────────►└─ offline_deployment_app.sh (app)
                                            force-recreates backend & frontend only
```

`postgres_data` is a named volume and **survives redeployments** — the image packs contain code only, no database data, so upgrades never touch business data.

## Connected side: prepare the image pack

### deploy_prepare.sh — merge + automatic tier selection

Must run on the `test` branch with a clean working tree:

```bash
bash scripts/deploy/deploy_prepare.sh                # auto tier selection (default)
bash scripts/deploy/deploy_prepare.sh --full         # force the full pack
bash scripts/deploy/deploy_prepare.sh --app          # force the app pack
bash scripts/deploy/deploy_prepare.sh --dry-run      # merge only, no packaging
bash scripts/deploy/deploy_prepare.sh --skip-merge   # skip the merge, package current HEAD
```

What the script does:

1. `git fetch origin main` (with 3 retries to work around OBS-mounted filesystem fetch quirks), then `merge --no-ff origin/main` into `test`; on conflict it exits — resolve manually and re-run with `--skip-merge`.
2. Picks the tier from the before/after merge diff:
   - touches `docker-compose.yml` / `Dockerfile*` / `requirements*.txt` / `opensandbox-config.toml` / `.env.example` / `alembic/` → **full pack** (`save_pack.sh`)
   - touches only `src/backend/`, `src/frontend/`, `mcp_servers/`, `configs/`, `prompts/` → **app pack** (`save_pack_app.sh`)
3. Produces `docker/hugagent-images-<sha>-<ts>.tar.gz` (full) or `docker/hugagent-app-images-<sha>-<ts>.tar.gz` (app), plus a `.manifest.txt` (image list + digests + sizes).

### save_pack.sh — full pack contents

Prerequisite: all self-built images already `docker compose build`-ed and all upstream images pulled; the script exits if any required image is missing.

| Category | Images |
|---|---|
| Self-built (required) | `hugagent-backend:latest`, `hugagent-mcp:latest`, `hugagent-frontend:latest` |
| Core infrastructure (required) | `postgres:15-alpine`, `redis:7-alpine` |
| opensandbox profile (required) | `opensandbox/server:v0.1.13`, `opensandbox/execd:v1.0.15`, `opensandbox/egress:v1.0.10`, `opensandbox/code-interpreter:v1.0.2` |
| mem0 profile (required) | `quay.io/coreos/etcd:v3.5.5`, `minio/minio:RELEASE.2023-03-13T19-46-17Z`, `milvusdb/milvus:v2.4.0`, `neo4j:5.15-community` |
| Optional (included when present locally) | `hugagent-script-runner:latest`, `hugagent-opensandbox-custom:latest` |

### save_pack_app.sh — incremental app pack

For releases that only touch application code (no Dockerfile / requirements / compose changes): the script runs `docker compose build backend mcp frontend` and then saves those three self-built images into the tarball.

## Production side: load and go live

The deployment directory on the production machine must match the layout the scripts expect: `offline_deployment*.sh` sits next to `docker-compose.yml`, with the tarballs in its `docker/` subdirectory (when run without arguments, the scripts pick the **newest** tarball there):

```
/opt/hugagent-deploy/
├── docker-compose.yml
├── .env                        # production config (HOST_STORAGE_PATH, tokens, model endpoint, ...)
├── offline_deployment.sh       # copied out of the repo's docker/ directory
├── offline_deployment_app.sh
└── docker/
    └── hugagent-images-<sha>-<ts>.tar.gz
```

### Full release

```bash
bash offline_deployment.sh                       # auto-picks the newest full pack under docker/
bash offline_deployment.sh <package_path>        # or specify explicitly
```

Internally: `gzip -dc <pkg> | docker load`, then

```bash
docker compose -f docker-compose.yml up -d --no-build --force-recreate --remove-orphans
```

(`COMPOSE_PROJECT_NAME=hugagent`; docker compose v2 and docker-compose v1 are auto-detected.)

### Incremental app release

```bash
bash offline_deployment_app.sh                   # auto-picks the newest hugagent-app-images-*.tar.gz
```

After `docker load` it only runs `up -d --no-build --force-recreate backend frontend`.

> ⚠️ Note: the app pack **contains a fresh `hugagent-mcp` image**, but `offline_deployment_app.sh` only recreates the backend and frontend containers. If the release changed code under `src/backend/mcp_servers/`, add one manual step:
> ```bash
> docker compose -f docker-compose.yml up -d --no-build --force-recreate mcp
> ```

### Data persistence

- Named volumes such as `postgres_data` are not removed by `--force-recreate`; database data persists across releases.
- File storage on the `${HOST_STORAGE_PATH}` bind mount persists as well.
- Database migrations run automatically in the backend entrypoint (`alembic upgrade head`) — the schema is upgraded as soon as the new image starts.

## Shipping prompt snapshots with the pack

System prompts live in the database `content_blocks` table (the `prompt_versions` pool plus the `prompt_hub` template gallery). They are **not in the repository and not in the images**. To push new prompts to production, ship them as a separate data file alongside the image pack and import on the production side:

### 1. Export on the connected side

```bash
python src/backend/scripts/export_content.py \
  --api-url http://localhost:3001 --only prompts
# outputs src/backend/scripts/exported/prompts_snapshot_<ts>.json
```

(`--database-url` is also supported for direct-DB export; `ADMIN_TOKEN` is read from `.env` automatically.)

### 2. Copy

Copy `prompts_snapshot_<ts>.json` to the production machine together with the image tarball.

### 3. Import on the production side

Prerequisite: the production backend image already includes the `POST /v1/content/prompts/import` endpoint (`src/backend/api/routes/v1/content.py`). Once the new backend is up:

```bash
docker cp prompts_snapshot_<ts>.json hugagent-backend:/tmp/
docker exec hugagent-backend curl -sS -X POST \
  'http://localhost:3001/v1/content/prompts/import?overwrite=true' \
  -H "Authorization: Bearer <production ADMIN_TOKEN>" \
  -H 'Content-Type: application/json' \
  -d @/tmp/prompts_snapshot_<ts>.json
```

The API import invalidates the prompt cache automatically — **no backend restart needed**. If the target backend predates the endpoint, fall back to `scripts/import_content.py --database-url ... --prompts <snapshot>` for a direct DB write, then restart the backend.

> This is an on-demand, one-time data operation: the DB volume persists, so prompts do not need re-importing on every release — only when pushing new prompts to production.

## Related source

| Feature | Files |
|---|---|
| Merge + auto tier selection | `scripts/deploy/deploy_prepare.sh` |
| Full image pack | `scripts/deploy/save_pack.sh` |
| Incremental app pack | `scripts/deploy/save_pack_app.sh` |
| Production full release | `scripts/deploy/offline_deployment.sh` |
| Production incremental release | `scripts/deploy/offline_deployment_app.sh` |
| Connected one-shot deploy (for comparison) | `scripts/deploy/local_deployment.sh` |
| Prompt export / import scripts | `src/backend/scripts/export_content.py`, `src/backend/scripts/import_content.py` |
| Prompt import API | `src/backend/api/routes/v1/content.py` (`GET /v1/content/prompts/export`, `POST /v1/content/prompts/import`) |
| Prompt version pool service | `src/backend/core/services/prompt_version_service.py` |
