# 环境变量参考

> 最后更新：2026-07-16 ｜ [English](../../en/deployment/environment-variables.md) ｜ 返回 [部署指南](README.md)

> 本文是 Docker Compose 部署的全量环境变量参考。**无 Docker 一键安装**的本地模式变量（`DEPLOY_PROFILE=local` 等）由安装脚本自动写入，另见 [无 Docker 一键安装](quick-install.md) 与 `.env.example` 末尾的「无 Docker 本地模式」段。

本文以 `.env.example` 与 `src/backend/core/config/settings.py` 为准，逐组列出全部环境变量。后端在进程启动时一次性读取环境（`settings` 单例），`.env` / `.env.<ENV>` 文件按「进程环境 > 环境专属文件 > 基础 `.env`」的优先级合并加载。`docker-compose.yml` 把 `.env` 变量注入各容器，部分变量在 compose 层另有默认值。

「版本」列：**CE** = 社区版即可用；**EE** = 商业版能力相关（功能边界见 [版本对比](../editions/overview.md)）。默认值列为 `.env.example` 示例值或代码内兜底值（标注「代码默认」）。

## 基础服务与端口

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `BACKEND_PORT` | `3001` | 后端监听端口 + Docker 端口映射；容器内同时作为 `PORT` | CE |
| `FRONTEND_PORT` | `3002` | 前端 nginx 对外端口（容器内 80） | CE |
| `VITE_API_BASE_URL` | （空） | 构建期烧进前端 JS 包的 API 基址；**留空走 nginx `/api` 反代（推荐）** | CE |
| `ENV` / `ENVIRONMENT` | `dev` | 运行环境（dev / staging / prod），影响日志格式与 `.env.<ENV>` 加载 | CE |
| `TZ` | `Asia/Shanghai` | 全部容器时区 | CE |
| `SERVICE_NAME` | `hugagent` | 服务名（日志 / 告警标识） | CE |
| `HOST_STORAGE_PATH` | `/var/lib/hugagent-storage` | 宿主机存储目录绝对路径，bind mount 进 backend/mcp 的 `/app/storage`；compose 强校验**必填** | CE |
| `HOST_REPO_PATH` | 仓库绝对路径 | 宿主机仓库根路径；backend 经 docker.sock 调宿主 daemon 重建沙盒镜像时解析 compose 相对路径用 | CE |
| `DOCKER_GID` | `999` | 宿主 docker 组 GID，授予 backend 容器读写 `/var/run/docker.sock`（`stat -c '%g' /var/run/docker.sock` 查询） | CE |
| `COMPOSE_PROFILES` | `script_runner` | compose profile 选择（`script_runner` / `opensandbox`，可叠加 `,mem0`），须与 `SANDBOX_PROVIDER` 一致 | CE |
| `COMPOSE_FILE` | （未设） | 叠加 compose 文件；CubeSandbox 时设 `docker-compose.yml:docker-compose.cube.yml` | CE |
| `MAX_REQUEST_SIZE` | `52428800`（代码默认 50 MB） | 后端单请求体上限（字节） | CE |
| `DB_MIGRATION_RETRIES` / `DB_MIGRATION_RETRY_INTERVAL` | `20` / `2`（入口脚本默认） | 启动迁移重试次数 / 间隔秒 | CE |

### 日志与会话看门狗

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR / CRITICAL | CE |
| `LOG_FORMAT` | `json`（compose 默认） | 日志格式 | CE |
| `LOG_TO_FILE` | `false` | 是否额外写文件日志 | CE |
| `LOG_FILE_PATH` | `/app/logs/backend.log` | 文件日志路径 | CE |
| `LOG_FILE_MAX_BYTES` | `10485760` | 单文件 10 MB 轮转 | CE |
| `LOG_FILE_BACKUP_COUNT` | `5` | 轮转保留份数 | CE |
| `CHAT_RUN_INACTIVITY_TIMEOUT_SEC` | `600` | 会话无输出超时即判卡死、落 failed | CE |
| `CHAT_RUN_MAX_AGE_SEC` | `1800` | running 超龄进入僵尸检查（叠加静默阈值，活跃长任务不杀） | CE |
| `CHAT_RUN_REAPER_INTERVAL_SEC` | `300` | 看门狗扫描间隔 | CE |
| `CHAT_RUN_STALE_QUIET_SEC` | 同 `CHAT_RUN_INACTIVITY_TIMEOUT_SEC` | 超龄 run 事件流静默超过此值才收成 failed | CE |
| `CHAT_RUN_HARD_MAX_AGE_SEC` | `21600` | 绝对寿命上限，超过即强制收割（即便仍在产出） | CE |

## 模型接入

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `MODEL_URL` | `http://your-model-host:3001/v1` | OpenAI 兼容主模型端点（**必填**） | CE |
| `API_KEY` | `your-api-key` | 主模型 API Key（**必填**） | CE |
| `BASE_MODEL_NAME` | `deepseek-chat` | 主对话模型名（**必填**） | CE |
| `QWEN_MODEL_NAME` | `qwen3_80b` | 辅助模型名（部分工具 / 分类用） | CE |
| `SUMMARIZE_MODEL_NAME` | `qwen3_80b` | 会话标题摘要模型 | CE |
| `ENABLE_SUMMARY` | `true` | 关闭则标题回退为消息截断 | CE |
| `SUMMARY_MAX_ROUNDS` | `3` | 超过该轮数后标题不再更新 | CE |
| `OPENAI_API_KEY` / `OPENAI_API_BASE` | （空）/ `https://api.openai.com/v1` | 备用 OpenAI 直连配置（compose 透传） | CE |
| `ROUTER_STRATEGY` | `main_only`（代码默认） | 路由策略（`orchestration/strategy.py`） | CE |
| `FOLLOWUP_ENABLED` | `true`（代码默认） | 追问建议生成开关 | CE |

## 鉴权与登录

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `AUTH_MODE` | `mock`（compose 默认） | `mock`（开发）/ `remote`（对接用户中心） | CE / remote 为 EE |
| `AUTH_API_URL` | （空） | 用户中心 API 地址（remote 模式） | EE |
| `AUTH_API_TIMEOUT` / `AUTH_RETRY_COUNT` | `5` / `2` | 用户中心调用超时（秒）/ 重试次数 | EE |
| `AUTH_MOCK_USER_ID` / `AUTH_MOCK_USERNAME` | `dev_user_001` / `Developer` | mock 模式固定用户 | CE |
| `LOCAL_AUTH_ENABLED` | `true`（代码默认） | 本地账号体系（注册 / 登录） | CE |
| `PASSWORD_MIN_LENGTH` | `8`（代码默认） | 本地账号密码最小长度 | CE |
| `INVITE_CODE_DEFAULT_TTL_HOURS` | `168`（代码默认） | 邀请码默认有效期（小时） | EE |
| `ADMIN_TOKEN` | （**必填**） | `/admin` 管理台与 `/v1/content/*` 写接口令牌 | CE |
| `CONFIG_TOKEN` | （**必填**） | `/config` 配置台与 `/v1/config/*`、`/v1/models/*` 等令牌 | CE |

### SSO 单点登录（商业版 EE）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SSO_LOGIN_MODE` | （空 → 自动判定 local/mock） | 登录页模式：`local` / `mock` / `remote` |
| `SSO_EXCHANGE_MODE` | `mock`（compose 默认） | 凭据交换模式，`remote` 才会调真实交换端点 |
| `SSO_MOCK_ENABLED` | `false` | 兼容旧配置的 mock 开关 |
| `SSO_TICKET_EXCHANGE_URL` | （空） | code/ticket → userInfo+token 交换端点 |
| `SSO_CALLBACK_PARAM` | `ticket` | 回调参数名（OAuth2 用 `code`） |
| `SSO_LOGIN_PROVIDER_URL` | （空） | 返回 `{data:{authorizeUrl}}` 的登录提供方接口 |
| `SSO_LOGIN_URL` | （空） | 401 兜底跳转 URL |
| `SSO_LOGOUT_URL` | （空） | 外部登出端点 |
| `SSO_TIMEOUT_SECONDS` | `5` | SSO 调用超时（秒） |
| `MOCK_SSO_APP_BASE` | （空） | mock SSO 回跳基址 |

### 会话 Cookie

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `SESSION_STORE` | `redis`（compose 默认；代码默认 `memory`） | 会话存储后端 | CE |
| `SESSION_TTL_HOURS` | `8` | 会话有效期（小时） | CE |
| `SESSION_COOKIE_NAME` | `jx_session` | Cookie 名 | CE |
| `SESSION_COOKIE_SECURE` | `false` | 生产 HTTPS 置 `true` | CE |
| `SESSION_COOKIE_HTTPONLY` | `true`（compose 默认） | 禁 JS 读取 | CE |
| `SESSION_COOKIE_SAMESITE` | `lax` | CSRF 防护 | CE |
| `SESSION_COOKIE_DOMAIN` | （空） | Cookie 域 | CE |

## 数据库与 Redis

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `DATABASE_URL` | compose 内拼为 `postgresql://hugagent_user:${DB_PASSWORD}@postgres:5432/hugagent`；代码兜底 `sqlite:///./hugagent.db` | 主库连接串 | CE |
| `DB_PASSWORD` | `hugagent_dev_password`（compose 默认） | PostgreSQL 密码（生产必改） | CE |
| `SQLITE_FALLBACK_URL` | `sqlite:///./hugagent_dev.db`（代码默认） | SQLite 兜底库 | CE |
| `DB_ECHO` | `false` | SQLAlchemy SQL 回显 | CE |
| `DB_POOL_SIZE` | `.env.example` 50；代码默认 20 | 连接池大小 | CE |
| `DB_POOL_MAX_OVERFLOW` | `10` | 连接池溢出上限 | CE |
| `DB_POOL_TIMEOUT` | `30` | 取连接超时（秒） | CE |
| `REDIS_URL` | `redis://redis:6379/0`（compose 默认） | Redis 连接串 | CE |
| `REDIS_SOCKET_TIMEOUT` | `30`（代码默认） | socket 读超时（秒），须大于流式 XREAD BLOCK 5s | CE |

## 存储

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `STORAGE_TYPE` | `local` | `local` / `s3` / `oss` | CE（s3 / oss 为 EE） |
| `STORAGE_PATH` | `./storage`（容器内固定 `/app/storage`） | 本地存储根目录 | CE |
| `S3_BUCKET` / `S3_REGION` / `S3_ENDPOINT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | （空）/ `us-east-1` / …… | S3 或兼容服务配置 | EE |
| `S3_CDN_DOMAIN` | （空） | CDN 加速域名 | EE |
| `S3_PRESIGNED_URL_EXPIRY` | `900` | 预签名 URL 有效期（秒） | EE |
| `OSS_ENDPOINT` / `OSS_BUCKET` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_KEY_PREFIX` | （空） | 阿里云 OSS 配置 | EE |
| `OSS_PRESIGNED_URL_EXPIRY` | `900` | OSS 预签名 URL 有效期（秒） | EE |
| `PROJECT_FILE_CAPACITY_BYTES` | `209715200`（200 MB） | 单项目 upload 类文件总量上限 | CE |

## 知识库与文件解析

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `KNOWLEDGE_BASE` | （空） | 设 `dify` 时把 Dify 数据集注入能力中心 | EE（Dify 对接） |
| `DIFY_URL` / `DIFY_API_KEY` | `http://your-dify-host:3001/v1` / …… | Dify 知识库 API | EE |
| `DIFY_ALLOWED_DATASET_IDS` | （空 = 全部） | 仅暴露指定数据集（逗号 / 换行 / 分号分隔） | EE |
| `KB_DETAIL_CONTENT_MAX_CHARS` | `50000`（代码默认） | KB 详情内容截断上限 | CE |
| `MILVUS_URL` | `http://milvus:19530` | 自建知识库 / 记忆共用的向量库地址 | CE |
| `MILVUS_TOKEN` | （空） | Milvus 鉴权 token | CE |
| `RERANKER_URL` / `RERANKER_API_KEY` | （示例值） | 重排模型服务（OpenAI 兼容） | CE |
| `RERANKER_MODEL` | （空 = 不启用） | 填入模型名即启用检索重排 | CE |
| `FILE_PARSER_API_URL` | （空） | 外部文件解析（OCR / 版面）服务地址 | CE |
| `FILE_PARSER_TIMEOUT` | `60` | 解析超时（秒） | CE |
| `FILE_PARSER_LANG_LIST` | `ch` | OCR 语言 | CE |
| `FILE_PARSER_BACKEND` / `FILE_PARSER_PARSE_METHOD` | `pipeline` / `auto` | 解析后端与方法 | CE |
| `FILE_PARSER_FORMULA_ENABLE` / `FILE_PARSER_TABLE_ENABLE` | `true` / `true` | 公式 / 表格解析开关 | CE |

## MCP 工具

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `MCP_HOST` | `mcp`（compose 默认） | MCP 容器主机名；本地调试可设 `127.0.0.1` | CE |
| `INTERNET_SEARCH_ENGINE` | `tavily`（compose 默认） | 联网搜索引擎选择 | CE |
| `TAVILY_API_KEY` | （**联网搜索必填**） | Tavily Search API Key | CE |
| `BAIDU_API_KEY` | （空） | 百度搜索 API Key | CE |
| `INTERNET_SEARCH_CN_ONLY` / `INTERNET_SEARCH_CN_STRICT` / `INTERNET_SEARCH_COUNTRY` / `INTERNET_SEARCH_AUTO_PARAMETERS` | （空） | 搜索地域 / 参数微调 | CE |
| `QUERY_DATABASE_URL` | `http://your-database-api-host:6200` | 数仓查询工具的 HTTP 后端 | EE（行业工具） |
| `QUERY_DATABASE_TIMEOUT_SECONDS` / `QUERY_DATABASE_RETRY_TIMES` / `QUERY_DATABASE_MAX_OUTPUT_TOKENS` | （空） | 数仓查询调用参数 | EE |
| `INDUSTRY_URL` / `INDUSTRY_AUTH_TOKEN` | （示例值） | 产业链信息 API | EE（行业工具） |
| `COMPANY_API_URL` / `COMPANY_AUTH_TOKEN` | （空） | 企业画像 API | EE（行业工具） |
| `BACKEND_INTERNAL_URL` | `http://backend:3001` | batch_runner 等 MCP 回调后端的内部地址 | CE |
| `BACKEND_INTERNAL_TOKEN` | （**批量执行必填**） | MCP → 后端回调令牌 | CE |
| `HUGAGENT_USER_SKILLS_DIR` / `HUGAGENT_PROJECT_SKILLS_DIR` | `~/.hugagent/skills` / `.hugagent/skills` | 技能目录覆盖 | CE |
| `HUGAGENT_DISABLE_USER_SKILLS` / `HUGAGENT_DISABLE_PROJECT_SKILLS` | `0` | 禁用用户 / 项目技能 | CE |

## 沙箱

### 通用

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `SANDBOX_PROVIDER` | `script_runner` | `script_runner` / `opensandbox` / `cube` | CE（持久沙箱 opensandbox/cube 为 EE） |
| `SANDBOX_TOOLS_ENABLED` | `false` | 是否给 Agent 注册 `bash` / `sandbox_put_artifact` / `sandbox_get_artifact` 三个工具 | CE |
| `SANDBOX_MAX_CONCURRENT` | `4` | 单进程并发沙盒执行数（预留） | CE |
| `SANDBOX_RUNNER_URL` | `http://hugagent-script-runner:8900` | script_runner sidecar 地址 | CE |
| `SANDBOX_TOOLS_TIMEOUT` / `SANDBOX_TOOLS_MAX_TIMEOUT` | `30` / `120` | 单条 bash 命令默认 / 最大超时（秒） | CE |
| `SANDBOX_TOOLS_MAX_MEMORY` | `256` | script_runner 内存上限（MB） | CE |
| `MYSPACE_WRITE_CONFIRM` | `true`（代码默认） | 沙盒对 `/myspace` 写操作须用户带外确认 | CE |

### OpenSandbox（持久沙箱，商业版 EE）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENSANDBOX_DOMAIN` | `http://opensandbox:8080` | OpenSandbox server 地址 |
| `OPENSANDBOX_API_KEY` | （空 = insecure 模式） | 上生产必须填强随机串 |
| `OPENSANDBOX_IMAGE` | `hugagent-opensandbox-custom:latest`（compose 默认；代码兜底 `opensandbox/code-interpreter:v1.0.2`） | 沙盒运行时镜像；自建镜像预装项目全部依赖（`docker/Dockerfile.opensandbox`） |
| `OPENSANDBOX_DEFAULT_TIMEOUT_S` | `1800` | 沙盒 TTL（秒），到期未续期自动销毁 |
| `OPENSANDBOX_READY_TIMEOUT_S` | `90` | 等沙盒就绪上限（秒） |
| `OPENSANDBOX_REQUEST_TIMEOUT_S` | `120` | 单次 HTTP 调用超时（秒） |
| `OPENSANDBOX_PORT` | `8910` | host 调试映射端口（容器内固定 8080） |
| `OPENSANDBOX_POOL_JUPYTER_MIN_IDLE` / `MAX_IDLE` | `.env.example` 1/3；compose 默认 2/3 | Jupyter 预热池（持久会话桶） |
| `OPENSANDBOX_POOL_LIGHT_MIN_IDLE` / `MAX_IDLE` | `2` / `5` | 轻量桶（一次性执行） |
| `OPENSANDBOX_POOL_MAX_TOTAL` | `20` | 全池上限（含使用中） |
| `OPENSANDBOX_IDLE_REAP_S` | `600`（compose 默认） | 持久会话空闲主动回收阈值（秒），`<=0` 关闭 |
| `OPENSANDBOX_SNAPSHOT_ENABLED` | `true` | 快照持久化总开关（idle 时 snapshot+kill，重连恢复） |
| `OPENSANDBOX_IDLE_SNAPSHOT_THRESHOLD_S` | `1500` | idle 超此值后台 snapshot + kill 让出资源（须小于沙盒 TTL） |
| `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS` | `7` | 快照保留天数（GC 周期清理） |
| `OPENSANDBOX_SNAPSHOT_WAIT_TIMEOUT_S` | `120` | 等快照 Ready 的轮询上限（秒） |
| `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED` | `true` | myspace 直挂：把 backend `myspace_cache/{uid}` bind 进沙盒 `/workspace/myspace/{uid}`，免 HTTP PUT 同步；`false` 回退全量 PUT 路径 |

### CubeSandbox（E2B 兼容 MicroVM，商业版 EE）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CUBE_NODE_IP` | （需填） | Cube 节点 IP（cube-dns sidecar 把 `*.cube.app` 解析到它） |
| `CUBE_API_URL` | `http://<node-ip>:38473` | 控制面 REST 地址 |
| `CUBE_API_KEY` | （空） | 控制面鉴权（CubeSandbox 默认 `e2b_000000`） |
| `CUBE_API_SANDBOX_DOMAIN` | `cube.app:38573` | 数据面沙盒域名（可带端口） |
| `CUBE_TEMPLATE` | （**必填**） | 沙盒模板 id |
| `CUBE_DEFAULT_TIMEOUT_S` / `CUBE_REQUEST_TIMEOUT_S` | `1800` / `120` | 沙盒 TTL / 单请求超时（秒） |
| `CUBE_CA_BUNDLE` | （空） | mkcert rootCA 容器内路径（注入 `SSL_CERT_FILE`） |
| `CUBE_IDLE_REAP_S` | `600` | 空闲主动回收阈值（秒），`<=0` 关闭 |
| `CUBE_POOL_MIN_IDLE` | `2` | 预热池目标空闲数，`<=0` 关闭 |
| `CUBE_OWNER_TAG` | （空 = 关闭孤儿清扫） | 环境 owner 标签；多环境共用节点时必须各设唯一值 |
| `CUBE_SKILL_PREPUSH` / `CUBE_SKILL_PREPUSH_MAX_MB` / `CUBE_SKILL_PREPUSH_CONCURRENCY` | `true` / `20` / `3` | 技能 tar 打包预推优化 |
| `CUBE_NODE_SSH_HOST` / `PORT` / `USER` / `KEY` | 回退 `CUBE_NODE_IP` / `22` / `root` / `/home/appuser/.ssh/cube_node_key` | 管理台「应用依赖」远程重建模板的 SSH 配置 |
| `CUBE_NODE_SSH_KEY_HOST` | `/home/<user>/.ssh/id_rsa` | 宿主机私钥路径（compose 只读挂载进容器） |
| `CUBE_BUILD_CTX_DIR` / `CUBE_BUILD_IMAGE_TAG` / `CUBE_BUILD_REGISTRY` | `/opt/cube-build` / `hugagent-cube-sandbox:latest` / `127.0.0.1:5000` | 节点构建上下文 / 镜像 tag / 本地 registry |
| `CUBE_BUILD_WRITABLE_LAYER` / `CUBE_BUILD_CPU` / `CUBE_BUILD_MEMORY` | `8Gi` / `2000` / `4000` | create-from-image 资源参数 |
| `CUBE_BUILD_EXPOSE_PORTS` / `CUBE_BUILD_PROBE_PORT` / `CUBE_BUILD_PROBE_PATH` | `49983,49999` / `49999` / `/health` | 模板端口与探针 |
| `CUBE_BUILD_TIMEOUT_S` / `CUBE_BUILD_REGISTER_TIMEOUT_S` | `1800` / `900` | 构建 / 注册超时（秒） |

## mem0 记忆系统

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `MEM0_ENABLED` | `false` | 总开关；`false` 时全部记忆路径零开销短路 | CE |
| `MEM0_GRAPH_ENABLED` | `false` | L3 图记忆开关（需 Neo4j） | CE |
| `MEM0_EMBED_URL` / `MEM0_EMBED_API_KEY` | （需填） | Embedding 服务（OpenAI 兼容） | CE |
| `MEM0_EMBED_MODEL` | `qwen3_embedding_8b` | Embedding 模型名 | CE |
| `MEM0_EMBED_DIMS` | `.env.example` 1024；compose 默认 4096 | 向量维度（须与模型一致） | CE |
| `MEMORY_MODEL_URL` / `MEMORY_API_KEY` / `MEMORY_MODEL_NAME` | 回退主模型配置 | 记忆抽取专用 LLM（不设则用主模型） | CE |
| `MILVUS_URL` / `MILVUS_TOKEN` | `http://milvus:19530` /（空） | 向量库 | CE |
| `NEO4J_URL` | `bolt://neo4j:7687` | Neo4j 地址 | CE |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` | `neo4j` / `hugagent_neo4j_2026`（compose 默认） | Neo4j 凭据 | CE |
| `MEMORY_LAYERED_ENABLED` | `true` | 分层记忆（L1 Profile / L2 Fact / L3 Graph）；`false` 回退扁平 mem0 | CE |
| `MEMORY_AUDIT_ENABLED` | `true` | 记忆审计表写入（合规留痕） | EE |
| `MEMORY_RETRIEVAL_BUDGET_MS` | `600` | Fact 向量检索预算（毫秒），超时只注入 Profile | CE |
| `MEMORY_BG_MAX_CONCURRENCY` | `8` | 后置抽取 / 保存任务并发上限 | CE |
| `MEMORY_EXTRACT_TIMEOUT_S` | `30` | 单次抽取 LLM 调用超时（秒） | CE |
| `MEMORY_PROFILE_MAX_CHARS` | `1500` | L1 Profile 字符上限（超出触发压缩） | CE |
| `MEMORY_FACT_DEFAULT_TTL_DAYS` | `180` | L2 Fact 默认 TTL（天） | CE |
| `MEMORY_FROZEN_TOPK` | `5` | 注入冻结块的 Fact top-K | CE |
| `MEMORY_BREAKER_THRESHOLD` / `MEMORY_BREAKER_COOLDOWN_S` | `3` / `60` | Milvus 熔断阈值 / 冷却（秒） | CE |

## 版本、品牌与 License

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `JX_EDITION` | `ee`（主仓默认；CE 派生树为 `ce`） | 版本门面：`ce` / `ee` | — |
| `BRAND_PRODUCT_NAME` | `.env.example` 示例为部署品牌；代码兜底 `智能体平台` | 产品显示名 | CE 可改 |
| `BRAND_ORG_NAME` | （示例值） | 组织名 | CE 可改 |
| `BRAND_POWERED_BY` | `true`（代码默认） | “Powered by” 署名显示；去署名需商业授权 | EE |
| `LICENSE_KEY_PATH` | （空） | license 文件容器内路径（Ed25519 签名，离线验签）；空且非强制模式 = 内部部署全功能 | EE |
| `JX_LICENSE_REQUIRED` | `false` | `true` = 私有化交付模式：无有效 license 即关闭全部 EE 能力位 | EE |
| `LICENSE_GRACE_DAYS` | `14` | 到期宽限期（天），宽限期内功能保留、探针报警 | EE |
| `LICENSE_PUBLIC_KEY` | （空 = 内置公钥） | 验签公钥覆盖（密钥轮换用） | EE |

License 细节见 [License 机制](../editions/license.md)。

## 其他（限流、熔断、安全、告警）

| 变量 | 默认值 | 说明 | 版本 |
|---|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | 接口限流开关 | CE |
| `RATE_LIMIT_STORAGE` | `memory://` | 限流计数存储（可 `redis://...`） | CE |
| `RATE_LIMIT_GLOBAL` / `RATE_LIMIT_PER_USER` | `500/minute` / `50/minute` | 全局 / 单用户限流 | CE |
| `CB_USER_CENTER_THRESHOLD` / `CB_USER_CENTER_TIMEOUT` | `5` / `60` | 用户中心熔断器 | EE |
| `CB_MODEL_API_THRESHOLD` / `CB_MODEL_API_TIMEOUT` | `10` / `30` | 模型 API 熔断器 | CE |
| `CB_STORAGE_THRESHOLD` / `CB_STORAGE_TIMEOUT` | `5` / `60` | 存储熔断器 | CE |
| `CORS_ORIGINS` | compose 默认 `http://localhost:3000,http://localhost:5173` | 允许跨域来源 | CE |
| `ENABLE_SECURITY_HEADERS` | （注释，建议生产 `true`） | 安全响应头 | CE |
| `AUDIT_LOG_RETENTION_DAYS` / `AUDIT_LOG_EXPORT_ENABLED` | `90` / `true`（注释示例） | 审计日志保留 / 导出 | EE |
| `ENABLE_LOG_MASKING` | （注释，建议 `true`） | 日志敏感信息脱敏 | CE |
| `ALERT_EMAIL_TO` / `ALERT_EMAIL_FROM` / `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | （注释） | 告警邮件 | CE |
| `PROMPT_PROVIDER` / `PROMPT_DIR` / `PROMPT_INLINE_TEMPLATE` / `JX_PROMPT_CONFIG` | `filesystem` /（空） | 提示词来源覆盖（默认 DB 优先、文件系统兜底） | CE |

## 相关源码

| 功能 | 文件 |
|---|---|
| 变量样例（部署起点） | `.env.example` |
| 集中式设置读取（唯一入口） | `src/backend/core/config/settings.py` |
| compose 层注入与默认值 | `docker-compose.yml`、`docker-compose.cube.yml` |
| 沙箱 provider 选择 | `src/backend/core/sandbox/`、`settings.py::SandboxSettings` |
| 记忆设置 | `settings.py::MemorySettings`、`src/backend/core/memory/`（service.py / pipeline.py） |
| License / 版本门面 | `settings.py::LicenseSettings / EditionSettings` |
