# Docker Compose 部署

> 最后更新：2026-07-16 ｜ [English](../../en/deployment/docker-compose.md) ｜ 返回 [部署指南](README.md)

> **适用场景**：团队 / 生产的**标准部署形态**，多用户、全功能。个人单机尝鲜可用更轻的 [无 Docker 一键安装](quick-install.md)。

HugAgentOS 的全部服务由根目录 `docker-compose.yml` 一个文件编排，按 profiles 分为常驻核心服务、互斥的沙箱 sidecar（`script_runner` / `opensandbox`）和可选的记忆基础设施（`mem0`）。本文给出完整服务拓扑、卷与持久化、profiles 用法及改代码后的 rebuild 流程。

## 服务拓扑

### 核心服务（始终启动）

| 服务 | 容器名 | 镜像 / 构建 | 端口（宿主→容器） | 作用 |
|---|---|---|---|---|
| `postgres` | hugagent-postgres | `postgres:15-alpine` | `5432:5432` | 主关系库（业务数据、content_blocks、用量日志） |
| `redis` | hugagent-redis | `redis:7-alpine` | `6380:6379` | 会话存储、流式 follower（Redis Streams）、限流 |
| `backend` | hugagent-backend | `docker/Dockerfile`（target `production`） | `${BACKEND_PORT:-3001}:同` | FastAPI 应用；启动时自动跑 alembic 迁移 |
| `mcp` | hugagent-mcp | `docker/Dockerfile.mcp` | 无对外端口 | 10 个 MCP server，以 streamable-http 监听 `9100–9108`、`9112`，backend 经 `http://mcp:91XX/mcp/` 调用 |
| `frontend` | hugagent-frontend | `src/frontend/Dockerfile` | `${FRONTEND_PORT:-3002}:80` | nginx 托管前端静态资源 + `/api` 反代到 backend |

### 沙箱 sidecar（profiles 二选一，互斥）

| 服务 | profile | 容器名 | 镜像 / 构建 | 作用 |
|---|---|---|---|---|
| `script-runner` | `script_runner` | hugagent-script-runner | `docker/Dockerfile.script-runner` | 轻量沙箱：1 GB 内存 / 1 CPU / read-only rootfs + tmpfs，技能目录只读挂载到 `/workspace/skills` |
| `opensandbox-config-init` | `opensandbox` | hugagent-opensandbox-config-init | `alpine:3.19` | 一次性 init：把 `docker/opensandbox-config.toml.tpl` 中的 `@@HOST_REPO_PATH@@` / `@@HOST_STORAGE_PATH@@` 渲染进 named volume `opensandbox_config` |
| `opensandbox` | `opensandbox` | hugagent-opensandbox | `opensandbox/server:v0.1.13` | 持久沙箱控制器（商业版 EE 能力）：经宿主 `docker.sock` 按需起停 sandbox 容器，Jupyter kernel 维持跨轮上下文；对外调试端口 `${OPENSANDBOX_PORT:-8910}:8080` |

### mem0 记忆基础设施（profile `mem0`，可选）

| 服务 | 容器名 | 镜像 | 端口 | 作用 |
|---|---|---|---|---|
| `etcd` | hugagent-etcd | `quay.io/coreos/etcd:v3.5.5` | 内部 | Milvus 元数据存储 |
| `minio` | hugagent-minio | `minio/minio:RELEASE.2023-03-13...` | 内部 | Milvus 对象存储 |
| `milvus` | hugagent-milvus | `milvusdb/milvus:v2.4.0` | `19530`、`9091` | 向量库（L2 向量记忆、自建知识库检索） |
| `neo4j` | hugagent-neo4j | `neo4j:5.15-community` | `7474`、`7687` | 图数据库（L3 知识图谱记忆，可选） |

### 依赖关系

```
frontend ──depends_on──► backend ──depends_on──► postgres (healthy)
                            │                    redis    (healthy)
                            │                    mcp      (started)
                            │
                            ├── (script_runner profile) ──► script-runner
                            └── (opensandbox profile)
                                  opensandbox ──depends_on──► opensandbox-config-init (完成)
mcp ──depends_on──► postgres (healthy)
milvus ──depends_on──► etcd (healthy) + minio (healthy)
```

所有服务接在同一 bridge 网络 `hugagent-network`，容器间用 service 名互访（如 `http://backend:3001`、`http://mcp:9102/mcp/`、`http://milvus:19530`）。每个容器统一应用 json-file 日志轮转（50 MB × 5 = 单容器 250 MB 封顶），防止日志写满磁盘。

## 卷与持久化

| 卷 / 挂载 | 挂到 | 内容 | 注意 |
|---|---|---|---|
| `postgres_data`（named volume） | postgres `/var/lib/postgresql/data` | 全部业务数据 | **跨重新部署保留**；删除即丢库 |
| `redis_data` | redis `/data` | AOF 持久化 | |
| `${HOST_STORAGE_PATH}`（**bind mount**） | backend、mcp `/app/storage` | 文件存储、myspace、sandbox_skills | 必须在 `.env` 设宿主机绝对路径；用 bind 而非 named volume 是为了让 OpenSandbox 沙盒能 host-bind 同一路径 |
| `manual_data` / `page_config_data` | backend + frontend | 手册 / 页面配置静态资源 | backend 写、frontend nginx 读 |
| `opensandbox_config` / `opensandbox_data` | opensandbox | 渲染后的 config.toml / 运行数据 | config 由 init 服务生成，opensandbox 只读挂载 |
| `etcd_data` / `minio_data` / `milvus_data` / `neo4j_data` / `neo4j_logs` | mem0 各服务 | 向量 / 图数据 | |
| `./src/backend`（bind） | backend、mcp `/app/src/backend` | 源码 | 源码热挂载：改 mcp 代码只需 `docker compose restart mcp`；backend 由 uvicorn 进程加载，改代码需重建/重启 |
| `/var/run/docker.sock`（bind） | backend、opensandbox | 宿主 docker daemon | backend 用于管理台「重建沙盒镜像」，需 `DOCKER_GID` 与宿主 docker 组一致 |

## Profiles 用法

profiles 由 `.env` 的 `COMPOSE_PROFILES` 控制（也可用命令行 `--profile`）：

```bash
# 默认：轻量沙箱
COMPOSE_PROFILES=script_runner

# 切换到 OpenSandbox 持久沙箱（须同步改 SANDBOX_PROVIDER=opensandbox）
COMPOSE_PROFILES=opensandbox

# 在沙箱之外叠加 mem0 记忆基础设施
COMPOSE_PROFILES=opensandbox,mem0
```

```bash
# 等价的命令行写法（一次性）
docker-compose --profile mem0 up -d
```

切换沙箱 provider 的完整流程（两个 sidecar 互斥，不要同时跑）：

```bash
# 1. 编辑 .env：SANDBOX_PROVIDER 与 COMPOSE_PROFILES 保持一致
# 2. 停旧
docker-compose down
# 3. 起新（自动 build 对应 Dockerfile）
docker-compose up -d --build
```

## 改代码后的 rebuild 流程

所有服务跑在容器里，**改代码后必须重建对应镜像并重启容器**才生效。

### 后端改动

```bash
docker-compose up -d --build backend
```

### MCP 工具改动

mcp 容器源码是 bind mount，重启即生效（FastMCP 不自动 reload）：

```bash
docker-compose restart mcp
```

### 前端改动

方式 A —— 完整重建（慢但稳）：

```bash
docker-compose up -d --build frontend
```

方式 B —— 本地构建后热替换进运行中的容器（快，需 Node 20+）：

```bash
cd src/frontend
npm run build
docker cp dist/. hugagent-frontend:/usr/share/nginx/html/
docker exec hugagent-frontend nginx -s reload
```

### 前后端都改了

```bash
docker-compose up -d --build backend frontend
```

### 强制清缓存重建（依赖变更、Dockerfile 改动）

`requirements*.txt` 或 Dockerfile 变更后若用了 cached layer 导致新代码没生效：

```bash
docker-compose build --no-cache backend frontend
docker-compose up -d backend frontend
```

> 验证容器内代码已更新：`docker exec hugagent-backend grep '<新代码关键串>' /app/src/backend/<文件>`。「改了没生效」90% 是 cached layer 没拉到新代码。

## 数据库迁移

backend 容器入口脚本自动迁移，常规部署无需手工操作：

- PostgreSQL：`alembic upgrade head`（失败重试，默认 20 次 × 2 s，可用 `DB_MIGRATION_RETRIES` / `DB_MIGRATION_RETRY_INTERVAL` 调整）
- SQLite（本地调试）：alembic 迁移含 PostgreSQL 专有 DDL，自动降级为 `Base.metadata.create_all()`

手工操作（开发新迁移时）：

```bash
# 容器内执行迁移
docker exec hugagent-backend alembic upgrade head

# 生成新迁移（本地，改完 core/db/models.py 后）
make migrate-new msg="describe change"
```

## 相关源码

| 功能 | 文件 |
|---|---|
| 服务编排 | `docker-compose.yml`（CubeSandbox 叠加：`docker-compose.cube.yml`） |
| 后端镜像 | `docker/Dockerfile`（multi-stage，target `production`） |
| MCP 镜像 | `docker/Dockerfile.mcp` |
| 沙箱镜像 | `docker/Dockerfile.script-runner`、`docker/Dockerfile.opensandbox` |
| 前端镜像 + nginx 反代 | `src/frontend/Dockerfile`、`src/frontend/nginx.conf`、`src/frontend/default.conf.template` |
| 后端入口（自动迁移） | `src/backend/scripts/backend_entrypoint.sh` |
| OpenSandbox 配置模板 | `docker/opensandbox-config.toml.tpl` |
| 迁移脚本 | `alembic.ini`、`src/backend/alembic/` |
| MCP 端口单一真源 | `src/backend/mcp_servers/_ports.py` |
