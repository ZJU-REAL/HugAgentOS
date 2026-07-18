# 离线生产部署（商业版 EE）

> 最后更新：2026-07-16 ｜ [English](../../en/deployment/offline-production.md) ｜ 返回 [部署指南](README.md)

> **适用场景**：政务 / 内网等**无法在线拉镜像的隔离环境**，镜像 tarball 离线交付。属商业版 EE 服务范畴。联网环境请用 [Docker Compose 部署](docker-compose.md)。

政务 / 内网等隔离环境无法在线拉镜像，HugAgentOS 采用「联网侧打镜像 tarball → 拷贝 → 生产侧 `docker load` + `compose up`」的离线交付流程。离线部署与专业实施属于商业版服务范畴（见 [版本对比](../editions/overview.md)）。本文以 `docker/` 目录下实际脚本为准。

## 流程总览

```
联网构建机（test 分支）                          离线生产机
─────────────────────────                       ─────────────────────────
scripts/deploy/deploy_prepare.sh
  ├─ git fetch + merge origin/main → test
  ├─ 按 diff 自动选档
  │    infra/依赖变更 → save_pack.sh（全量包）
  │    仅应用代码     → save_pack_app.sh（app 包）
  └─ 产出 docker/hugagent-*-<sha>-<ts>.tar.gz
            + 同名 .manifest.txt
                    │
                scp 拷贝                ┌─ offline_deployment.sh（全量）
                    ├──────────────────►│    docker load + compose up -d
                    │                   │    --no-build --force-recreate
（可选）提示词快照                       │    --remove-orphans
prompts_snapshot_<ts>.json ────────────►└─ offline_deployment_app.sh（app）
                                             仅 force-recreate backend frontend
```

`postgres_data` 是 named volume，**跨重新部署保留**——镜像包里只有代码、没有数据库数据，升级不会动业务数据。

## 联网侧：准备镜像包

### deploy_prepare.sh —— 合并 + 自动选档

必须在 `test` 分支、工作树干净的前提下执行：

```bash
bash scripts/deploy/deploy_prepare.sh                # 自动选档（默认）
bash scripts/deploy/deploy_prepare.sh --full         # 强制全量包
bash scripts/deploy/deploy_prepare.sh --app          # 强制 app 包
bash scripts/deploy/deploy_prepare.sh --dry-run      # 只 merge 不打包
bash scripts/deploy/deploy_prepare.sh --skip-merge   # 跳过 merge，对当前 HEAD 打包
```

脚本行为：

1. `git fetch origin main`（带 3 次重试，规避 OBS 挂载文件系统的 fetch 异常）后 `merge --no-ff origin/main` 进 `test`；冲突时退出，手动解决后以 `--skip-merge` 重跑。
2. 按 merge 前后 diff 自动选档：
   - 命中 `docker-compose.yml` / `Dockerfile*` / `requirements*.txt` / `opensandbox-config.toml` / `.env.example` / `alembic/` → **全量包**（`save_pack.sh`）
   - 仅命中 `src/backend/`、`src/frontend/`、`mcp_servers/`、`configs/`、`prompts/` → **app 包**（`save_pack_app.sh`）
3. 产出 `docker/hugagent-images-<sha>-<ts>.tar.gz`（全量）或 `docker/hugagent-app-images-<sha>-<ts>.tar.gz`（app），附 `.manifest.txt`（镜像清单 + digest + 大小）。

### save_pack.sh —— 全量包内容

前置：自建镜像已 `docker compose build`、上游镜像已 `docker pull`，缺一个直接退出。

| 类别 | 镜像 |
|---|---|
| 自建（必备） | `hugagent-backend:latest`、`hugagent-mcp:latest`、`hugagent-frontend:latest` |
| 核心基础设施（必备） | `postgres:15-alpine`、`redis:7-alpine` |
| opensandbox profile（必备） | `opensandbox/server:v0.1.13`、`opensandbox/execd:v1.0.15`、`opensandbox/egress:v1.0.10`、`opensandbox/code-interpreter:v1.0.2` |
| mem0 profile（必备） | `quay.io/coreos/etcd:v3.5.5`、`minio/minio:RELEASE.2023-03-13T19-46-17Z`、`milvusdb/milvus:v2.4.0`、`neo4j:5.15-community` |
| 可选（本地存在则带上） | `hugagent-script-runner:latest`、`hugagent-opensandbox-custom:latest` |

### save_pack_app.sh —— 应用增量包

适合只改了应用代码（不动 Dockerfile / requirements / compose）的发版：脚本内先 `docker compose build backend mcp frontend`，再把这三个自建镜像打成 tarball。

## 生产侧：加载与上线

生产机部署目录需与脚本约定的布局一致：`offline_deployment*.sh` 与 `docker-compose.yml` 同级，镜像包放在其 `docker/` 子目录（脚本不带参数时自动取该目录下**最新**的 tarball）：

```
/opt/hugagent-deploy/
├── docker-compose.yml
├── .env                        # 生产配置（HOST_STORAGE_PATH、各 token、模型地址…）
├── offline_deployment.sh       # 从仓库 docker/ 拷出
├── offline_deployment_app.sh
└── docker/
    └── hugagent-images-<sha>-<ts>.tar.gz
```

### 全量上线

```bash
bash offline_deployment.sh                       # 自动取 docker/ 下最新全量包
bash offline_deployment.sh <package_path>        # 或显式指定
```

内部执行：`gzip -dc <pkg> | docker load`，然后

```bash
docker compose -f docker-compose.yml up -d --no-build --force-recreate --remove-orphans
```

（`COMPOSE_PROJECT_NAME=hugagent`；docker compose v2 与 docker-compose v1 自动探测。）

### 应用增量上线

```bash
bash offline_deployment_app.sh                   # 自动取最新 hugagent-app-images-*.tar.gz
```

内部 `docker load` 后仅 `up -d --no-build --force-recreate backend frontend`。

> ⚠️ 注意：app 包里**含 `hugagent-mcp` 新镜像**，但 `offline_deployment_app.sh` 只重建 backend / frontend 两个容器。若本次发版改了 `src/backend/mcp_servers/` 代码，需手动补一句：
> ```bash
> docker compose -f docker-compose.yml up -d --no-build --force-recreate mcp
> ```

### 数据持久化

- `postgres_data` 等 named volume 不随 `--force-recreate` 删除，数据库数据跨发版保留。
- `${HOST_STORAGE_PATH}` bind mount 的文件存储同样保留。
- 数据库迁移由 backend 容器入口脚本自动执行（`alembic upgrade head`），新镜像起来即完成 schema 升级。

## 提示词快照随包带入

系统提示词存在数据库 `content_blocks` 表（`prompt_versions` 版本池 + `prompt_hub` 提示词广场），**不进仓库、不在镜像里**。要把新提示词推上生产，需作为单独数据文件随镜像包带入并在生产侧导入：

### 1. 联网侧导出

```bash
python src/backend/scripts/export_content.py \
  --api-url http://localhost:3001 --only prompts
# 产出 src/backend/scripts/exported/prompts_snapshot_<ts>.json
```

（也支持 `--database-url` 直连库导出；`ADMIN_TOKEN` 自动从 `.env` 读取。）

### 2. 拷贝

把 `prompts_snapshot_<ts>.json` 与镜像 tarball 一起拷到生产机。

### 3. 生产侧导入

前提：生产 backend 镜像须已包含 `POST /v1/content/prompts/import` 接口（`src/backend/api/routes/v1/content.py`）。在新 backend 起好后：

```bash
docker cp prompts_snapshot_<ts>.json hugagent-backend:/tmp/
docker exec hugagent-backend curl -sS -X POST \
  'http://localhost:3001/v1/content/prompts/import?overwrite=true' \
  -H "Authorization: Bearer <生产ADMIN_TOKEN>" \
  -H 'Content-Type: application/json' \
  -d @/tmp/prompts_snapshot_<ts>.json
```

API 导入会自动失效提示词缓存，**无需重启 backend**。若目标后端还没有该接口，可改用 `scripts/import_content.py --database-url ... --prompts <snapshot>` 直连数据库写入，导入后重启 backend。

> 这是按需的一次性数据操作：DB 卷持久化，不必每次发版都导提示词——只在要把新提示词推上生产时执行。

## 相关源码

| 功能 | 文件 |
|---|---|
| 合并 + 自动选档 | `scripts/deploy/deploy_prepare.sh` |
| 全量镜像包 | `scripts/deploy/save_pack.sh` |
| 应用增量包 | `scripts/deploy/save_pack_app.sh` |
| 生产侧全量上线 | `scripts/deploy/offline_deployment.sh` |
| 生产侧增量上线 | `scripts/deploy/offline_deployment_app.sh` |
| 联网环境一键部署（对照） | `scripts/deploy/local_deployment.sh` |
| 提示词导出 / 导入脚本 | `src/backend/scripts/export_content.py`、`src/backend/scripts/import_content.py` |
| 提示词导入 API | `src/backend/api/routes/v1/content.py`（`GET /v1/content/prompts/export`、`POST /v1/content/prompts/import`） |
| 提示词版本池服务 | `src/backend/core/services/prompt_version_service.py` |
