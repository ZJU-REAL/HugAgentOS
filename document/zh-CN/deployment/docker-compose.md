# Docker Compose 部署

> 最后更新：2026-07-23 ｜ [English](../../en/deployment/docker-compose.md) ｜ 返回 [部署指南](README.md)

> **适用场景**：团队 / 生产的**标准部署形态**，多用户、全功能。个人单机尝鲜可用更轻的 [无 Docker 一键安装](quick-install.md)。

HugAgentOS 的全部服务由根目录 `docker-compose.yml` 一个文件编排，按 profiles 分为常驻核心服务、互斥的沙箱 sidecar（`script_runner` / `opensandbox`）和可选的记忆基础设施（`mem0`）。本文给出完整服务拓扑、卷与持久化、profiles 用法及改代码后的 rebuild 流程。


## CE Docker 默认沙盒

CE 的 Docker 部署默认使用 OpenSandbox；无 Docker 安装及桌面本机模式仍使用本地执行。
首次安装执行：

```bash
python3 scripts/configure-ce-docker.py
docker build -f docker/sandbox/Dockerfile.opensandbox -t hugagent-opensandbox-custom:latest .
docker compose build backend
docker compose up -d --build
```

初始化脚本生成绝对宿主路径、项目名和随机控制器密钥，并拒绝覆盖已有 .env。
可使用 --project-name 和 --storage 指定项目名与存储目录。
控制器网络名按项目名生成，控制器不发布宿主端口。控制器通过 Docker socket 管理沙盒；
后端保留原有管理 socket 挂载，CE 不提供沙盒重建管理接口。
运行镜像必须先构建；普通 compose build 不会构建按需运行的沙盒镜像。
SDK、控制器、execd、egress 与运行镜像按仓库配置验证，不随本次调整升级上游版本。

已有部署需要人工修改现有 .env：设置 SANDBOX_PROVIDER=opensandbox、
COMPOSE_PROFILES=opensandbox、SANDBOX_TOOLS_ENABLED=true，填写绝对 HOST_REPO_PATH、
HOST_STORAGE_PATH 和强随机 OPENSANDBOX_API_KEY，并显式设置 COMPOSE_PROJECT_NAME。
保留原存储目录和数据卷。旧 script-runner 工作区不会自动迁入 OpenSandbox。
按部署授权流程停止旧 runner、构建运行镜像并启动新控制器及后端。
检查 /health、/ready、连续命令文件保留、用户隔离、后端重启接管和空闲快照恢复。
初始化或依赖失败会报错，不会自动切换 provider。
OpenSandbox 保留会话文件与快照；每次 Python/JS 命令启动独立进程，不保留内存变量。


空间对象挂载由每个沙盒独立的挂载服务管理，控制器不持有 FUSE 连接。
首次挂载和恢复使用同一流程；控制器重启不会中断该连接，沙盒重启后会按当前授权检查并恢复。
不需要在宿主机准备共享传播目录。只有挂载服务需要 `/dev/fuse` 和挂载权限。
旧版创建的沙盒若含 FUSE 宿主 bind 配置，升级不能原地修改其 Docker 启动参数；
须先保全工作区和数据卷，再单独安排迁移，不直接重启或删除这些旧沙盒。

## 服务拓扑

### 核心服务（始终启动）

| 服务 | 容器名 | 镜像 / 构建 | 端口（宿主→容器） | 作用 |
|---|---|---|---|---|
| `postgres` | hugagent-postgres | `postgres:15-alpine` | `${POSTGRES_HOST_PORT:-5432}:5432` | 主关系库（业务数据、content_blocks、用量日志） |
| `redis` | hugagent-redis | `redis:7-alpine` | `${REDIS_HOST_PORT:-6380}:6379` | 会话存储、流式 follower（Redis Streams）、限流 |
| `backend` | hugagent-backend | `docker/Dockerfile`（target `production`） | `${BACKEND_HOST_PORT:-3001}:${BACKEND_PORT:-3001}` | FastAPI 应用；启动时自动跑 alembic 迁移 |
| `mcp` | hugagent-mcp | `docker/Dockerfile.mcp` | 无对外端口 | CE 启动通用 MCP 进程；EE 另启动数据库查询等商业 MCP。backend 经 `http://mcp:91XX/mcp/` 调用 |
| `frontend` | hugagent-frontend | `src/frontend/Dockerfile` | `${FRONTEND_PORT:-3002}:80` | nginx 托管前端静态资源 + `/api` 反代到 backend |

`BACKEND_PORT` 是容器内的监听端口，nginx、MCP 和健康检查都依赖它，通常保持 `3001`。如果宿主机端口已被占用，只调整 `BACKEND_HOST_PORT`、`POSTGRES_HOST_PORT` 或 `REDIS_HOST_PORT`，不要改容器内端口。例如可将后端公开到 `13003`，同时保持容器内的 `3001`。

CE 在空数据库首次启动时会全局安装并启用 `automation`、
`skill-manager` 和 `sites` 三个插件，所有用户可直接使用，无需分别前往
插件市场安装。CE 的能力页、运行时 catalog、MCP 端口表和 MCP 镜像均不包含
数据库查询工具；该能力仅存在于标注为 EE 的商业版部署。

### 沙箱 sidecar（profiles 二选一，互斥）

| 服务 | profile | 容器名 | 镜像 / 构建 | 作用 |
|---|---|---|---|---|
| `script-runner` | `script_runner` | hugagent-script-runner | `docker/sandbox/Dockerfile.script-runner` | 轻量沙箱：1 GB 内存 / 1 CPU / read-only rootfs + tmpfs，技能目录只读挂载到 `/workspace/skills` |
| `opensandbox-config-init` | `opensandbox` | hugagent-opensandbox-config-init | `python:3.12-alpine` | 一次性 init：把 `docker/opensandbox-server/opensandbox-config.toml.tpl` 中的 `@@HOST_REPO_PATH@@` / `@@HOST_STORAGE_PATH@@` 渲染进 named volume `opensandbox_config` |
| `opensandbox` | `opensandbox` | hugagent-opensandbox | `hugagent-opensandbox-server:v0.1.13-3` | CE / EE 共享控制器：经 Docker socket 管理沙盒；execd 执行命令并保留会话文件。CE 不映射宿主端口；全量仓库可配置调试端口 |

### mem0 记忆基础设施（profile `mem0`，可选）

| 服务 | 容器名 | 镜像 | 端口 | 作用 |
|---|---|---|---|---|
| `etcd` | hugagent-etcd | `quay.io/coreos/etcd:v3.5.5` | 内部 | Milvus 元数据存储 |
| `minio` | hugagent-minio | `minio/minio:RELEASE.2023-03-13...` | 内部 | Milvus 对象存储 |
| `milvus` | hugagent-milvus | `milvusdb/milvus:v2.5.4` | `19530`、`9091` | 向量库（L2 向量记忆、自建知识库检索） |
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

Compose 基础服务接在同一 bridge 网络 `hugagent-network`，容器间用 service 名互访（如 `http://backend:3001`、`http://mcp:9102/mcp/`、`http://milvus:19530`）。每个容器统一应用 json-file 日志轮转（50 MB × 5 = 单容器 250 MB 封顶），防止日志写满磁盘。

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
# 全量仓库默认：轻量沙箱；CE Docker 默认 opensandbox
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

> **说明：** 首次构建会下载钉钉、飞书、邮件和浏览器渲染等可选工具。
> 每项下载都有重试与总超时；若某个可选源持续不可用，核心后端与 Python/bash
> 沙箱仍会完成构建，构建日志会明确指出暂不可用的集成。

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
| 服务编排 | `docker-compose.yml`（CubeSandbox 叠加：`docker/sandbox/compose.cube.yml`） |
| 后端镜像 | `docker/Dockerfile`（multi-stage，target `production`） |
| MCP 镜像 | `docker/Dockerfile.mcp` |
| 沙箱镜像 | `docker/sandbox/Dockerfile.script-runner`、`docker/sandbox/Dockerfile.opensandbox` |
| 前端镜像 + nginx 反代 | `src/frontend/Dockerfile`、`src/frontend/nginx.conf`、`src/frontend/default.conf.template` |
| 后端入口（自动迁移） | `src/backend/scripts/backend_entrypoint.sh` |
| OpenSandbox 配置模板 | `docker/opensandbox-server/opensandbox-config.toml.tpl` |
| 迁移脚本 | `alembic.ini`、`src/backend/alembic/` |
| MCP 端口单一真源 | `src/backend/extensions/mcp_servers/_ports.py` |

### OpenSandbox 命令延迟与控制器镜像

控制器使用 `docker/opensandbox-server/Dockerfile.opensandbox-server` 构建
`hugagent-opensandbox-server:v0.1.13-3`。它基于固定的上游版本，对已知沙盒使用
按名称 inspect 并校验归属标签，且在线程池中执行生命周期与代理的同步 Docker 查询，
避免阻塞其它请求。构建时先校验上游源文件摘要并运行回归测试；升级上游版本时须重新审查补丁。
离线交付必须包含此控制器镜像，不能仅携带原始 `opensandbox/server` 镜像。

后端直连优化同时更新命令、文件、健康检查等 SDK 客户端的 endpoint 与 HTTP 基地址；
状态轮询和增量日志不再绕回代理。直连不可达或 SDK 客户端结构不受支持时保留代理。
首次应用这项修复需要重建控制器镜像并重启后端与控制器；遵守部署授权流程，
应用后分别验证同会话连续命令、后端重启恢复和并发请求。

## 站点数据与浏览器部署检查

普通 Compose 启动不启用应用数据托管，也不重建 OpenSandbox 按需运行镜像。需要这两项能力时，部署前分别完成[应用数据升级](../modules/application-hosting.md#部署要求与数据安全)和[浏览器运行镜像检查](../modules/browser-automation.md)。配置不同的 APPLICATION_DB_PASSWORD 与 APPLICATION_OWNER_DB_PASSWORD、共享 Redis 和适合网络环境的 BROWSER_DNS_RESOLVER_URL；使用 application_owner 作为应用数据运行账号。已有数据库卷也必须重新执行 application-database-init，然后更新后端、MCP、前端及沙箱运行镜像，并创建新浏览器会话进行验证。

### WSL 内存紧张时限制构建资源

在 WSL 项目目录中创建专用 BuildKit builder，限制内存及构建并发：

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

builder 已存在时直接复用。构建结束后停止专用 builder，保留缓存供下次使用。
应用托管部署仍需叠加 docker/application-hosting/compose.application-hosting.yml。
不要同时运行完整构建和多组浏览器测试。WSL 上限应结合其他容器及编辑器的实际占用设置。

## 模型网关启动与排障（商业版 EE）

模型网关使用独立的 `litellm` 服务和 `gateway` profile。仅设置
`MODEL_GATEWAY_ENABLED=true` 不会启动该容器。需要网关时，在 `.env` 的
现有 `COMPOSE_PROFILES` 列表中追加 `gateway`，保留其他 profile。
后端管理地址使用同一 Docker 网络中的 `http://litellm:4000`。

已有网关数据库和密钥的环境可按以下步骤恢复服务：

```bash
docker compose --profile gateway up -d --no-deps litellm
docker compose ps litellm
docker compose exec backend getent hosts litellm
```

上述恢复命令要求 PostgreSQL 已健康运行。等待 LiteLLM 完成启动迁移并变为
healthy，再在管理台验证用量统计和调用日志。保留原有 `litellm` 数据库与
`LITELLM_MASTER_KEY`，不要通过清空数据或换钥恢复连接。

出现 `Name or service not known` 时，先核查容器是否存在、是否与后端处于
同一网络，以及 `LITELLM_ADMIN_URL` 是否正确。缺失的服务名可能被外部 DNS
返回代理地址；能解析出地址并不代表网关容器存在。

### 沙盒共享网络

沙盒恢复使用控制器配置的共享 Docker 网络。创建、查询、接管、删除和到期回收
均不再创建临时网络，也不接入或断开后端、控制器的网络端点。
此回退不要求升级 Docker 或 Compose。控制器仍按配置网络限定实例查询和回收范围。
沙盒间恢复共享网络可达性及上游 direct 模式的宿主端口映射，不能将其称为网络隔离。

已有独立网络中的沙盒不自动迁移或删除。部署前记录其文件、挂载和快照，
按授权安排会话停机与重建，保留工作区和数据卷；不要直接删除网络来强制恢复。
此变更只影响云端控制器，无需桌面客户端发版。
