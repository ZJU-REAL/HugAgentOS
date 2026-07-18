# Environment Variable Reference

> Last updated: 2026-07-16 ｜ [简体中文](../../zh-CN/deployment/environment-variables.md) ｜ Back to [Deployment Guide](README.md)

> This document is the full environment-variable reference for the Docker Compose deployment. The **No-Docker Quick Install** local-mode variables (`DEPLOY_PROFILE=local`, etc.) are written automatically by the installer; see [No-Docker Quick Install](quick-install.md) and the "No-Docker local mode" section at the end of `.env.example`.

This reference is sourced from `.env.example` and `src/backend/core/config/settings.py`, listing every environment variable by group. The backend reads the environment once at process startup (the `settings` singleton); `.env` / `.env.<ENV>` files are merged with the precedence "process env > env-specific file > base `.env`". `docker-compose.yml` injects `.env` variables into each container, and some variables carry additional compose-level defaults.

"Edition" column: **CE** = usable in the Community Edition; **EE** = tied to Enterprise Edition capabilities (see [Edition Comparison](../editions/overview.md) for the boundary). The default column shows the `.env.example` sample value or the in-code fallback (marked "code default").

## Core services and ports

| Variable | Default | Description | Edition |
|---|---|---|---|
| `BACKEND_PORT` | `3001` | Backend listen port + Docker port mapping; also becomes `PORT` inside the container | CE |
| `FRONTEND_PORT` | `3002` | Frontend nginx host port (80 inside the container) | CE |
| `VITE_API_BASE_URL` | (empty) | API base URL baked into the frontend JS bundle at build time; **leave empty to use the nginx `/api` proxy (recommended)** | CE |
| `ENV` / `ENVIRONMENT` | `dev` | Runtime environment (dev / staging / prod); affects log format and `.env.<ENV>` loading | CE |
| `TZ` | `Asia/Shanghai` | Timezone for all containers | CE |
| `SERVICE_NAME` | `hugagent` | Service name (logging / alerting identifier) | CE |
| `HOST_STORAGE_PATH` | `/var/lib/hugagent-storage` | Host absolute path of the storage directory, bind-mounted into backend/mcp at `/app/storage`; **required**, enforced by compose | CE |
| `HOST_REPO_PATH` | repo absolute path | Repository root on the host; used when the backend resolves compose-relative paths through docker.sock to rebuild sandbox images | CE |
| `DOCKER_GID` | `999` | Host docker group GID granting the backend container access to `/var/run/docker.sock` (find with `stat -c '%g' /var/run/docker.sock`) | CE |
| `COMPOSE_PROFILES` | `script_runner` | Compose profile selection (`script_runner` / `opensandbox`, append `,mem0` as needed); must match `SANDBOX_PROVIDER` | CE |
| `COMPOSE_FILE` | (unset) | Compose overlay files; set `docker-compose.yml:docker-compose.cube.yml` for CubeSandbox | CE |
| `MAX_REQUEST_SIZE` | `52428800` (code default, 50 MB) | Maximum request body size (bytes) | CE |
| `DB_MIGRATION_RETRIES` / `DB_MIGRATION_RETRY_INTERVAL` | `20` / `2` (entrypoint defaults) | Startup migration retry count / interval (seconds) | CE |

### Logging and chat-run watchdog

| Variable | Default | Description | Edition |
|---|---|---|---|
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR / CRITICAL | CE |
| `LOG_FORMAT` | `json` (compose default) | Log format | CE |
| `LOG_TO_FILE` | `false` | Write an additional file log | CE |
| `LOG_FILE_PATH` | `/app/logs/backend.log` | File log path | CE |
| `LOG_FILE_MAX_BYTES` | `10485760` | 10 MB rotation per file | CE |
| `LOG_FILE_BACKUP_COUNT` | `5` | Rotated files kept | CE |
| `CHAT_RUN_INACTIVITY_TIMEOUT_SEC` | `600` | A run with no output for this long is declared stuck and marked failed | CE |
| `CHAT_RUN_MAX_AGE_SEC` | `1800` | Running runs older than this become zombie-check candidates (combined with the quiet threshold; actively streaming runs survive) | CE |
| `CHAT_RUN_REAPER_INTERVAL_SEC` | `300` | Watchdog sweep interval | CE |
| `CHAT_RUN_STALE_QUIET_SEC` | same as `CHAT_RUN_INACTIVITY_TIMEOUT_SEC` | An over-age run is only reaped after its event stream has been quiet this long | CE |
| `CHAT_RUN_HARD_MAX_AGE_SEC` | `21600` | Absolute lifetime ceiling — reaped even while still producing output | CE |

## Model access

| Variable | Default | Description | Edition |
|---|---|---|---|
| `MODEL_URL` | `http://your-model-host:3001/v1` | OpenAI-compatible primary model endpoint (**required**) | CE |
| `API_KEY` | `your-api-key` | Primary model API key (**required**) | CE |
| `BASE_MODEL_NAME` | `deepseek-chat` | Primary chat model name (**required**) | CE |
| `QWEN_MODEL_NAME` | `qwen3_80b` | Auxiliary model (some tools / classification) | CE |
| `SUMMARIZE_MODEL_NAME` | `qwen3_80b` | Conversation-title summarization model | CE |
| `ENABLE_SUMMARY` | `true` | When off, titles fall back to message truncation | CE |
| `SUMMARY_MAX_ROUNDS` | `3` | Titles stop updating after this many rounds | CE |
| `OPENAI_API_KEY` / `OPENAI_API_BASE` | (empty) / `https://api.openai.com/v1` | Alternate direct OpenAI configuration (passed through by compose) | CE |
| `ROUTER_STRATEGY` | `main_only` (code default) | Routing strategy (`orchestration/strategy.py`) | CE |
| `FOLLOWUP_ENABLED` | `true` (code default) | Follow-up suggestion generation | CE |

## Authentication and login

| Variable | Default | Description | Edition |
|---|---|---|---|
| `AUTH_MODE` | `mock` (compose default) | `mock` (development) / `remote` (external user center) | CE / remote is EE |
| `AUTH_API_URL` | (empty) | User-center API URL (remote mode) | EE |
| `AUTH_API_TIMEOUT` / `AUTH_RETRY_COUNT` | `5` / `2` | User-center call timeout (s) / retries | EE |
| `AUTH_MOCK_USER_ID` / `AUTH_MOCK_USERNAME` | `dev_user_001` / `Developer` | Fixed user in mock mode | CE |
| `LOCAL_AUTH_ENABLED` | `true` (code default) | Local account system (sign-up / sign-in) | CE |
| `PASSWORD_MIN_LENGTH` | `8` (code default) | Minimum local-account password length | CE |
| `INVITE_CODE_DEFAULT_TTL_HOURS` | `168` (code default) | Default invite-code validity (hours) | EE |
| `ADMIN_TOKEN` | (**required**) | Token for the `/admin` console and `/v1/content/*` write APIs | CE |
| `CONFIG_TOKEN` | (**required**) | Token for the `/config` console and `/v1/config/*`, `/v1/models/*`, etc. | CE |

### SSO single sign-on (Enterprise Edition)

| Variable | Default | Description |
|---|---|---|
| `SSO_LOGIN_MODE` | (empty → auto local/mock) | Login-page mode: `local` / `mock` / `remote` |
| `SSO_EXCHANGE_MODE` | `mock` (compose default) | Credential exchange mode; only `remote` calls the real exchange endpoint |
| `SSO_MOCK_ENABLED` | `false` | Legacy mock switch |
| `SSO_TICKET_EXCHANGE_URL` | (empty) | code/ticket → userInfo+token exchange endpoint |
| `SSO_CALLBACK_PARAM` | `ticket` | Callback parameter name (`code` for OAuth2) |
| `SSO_LOGIN_PROVIDER_URL` | (empty) | Login provider endpoint returning `{data:{authorizeUrl}}` |
| `SSO_LOGIN_URL` | (empty) | 401 fallback redirect URL |
| `SSO_LOGOUT_URL` | (empty) | External logout endpoint |
| `SSO_TIMEOUT_SECONDS` | `5` | SSO call timeout (s) |
| `MOCK_SSO_APP_BASE` | (empty) | Mock-SSO return base URL |

### Session cookies

| Variable | Default | Description | Edition |
|---|---|---|---|
| `SESSION_STORE` | `redis` (compose default; code default `memory`) | Session store backend | CE |
| `SESSION_TTL_HOURS` | `8` | Session lifetime (hours) | CE |
| `SESSION_COOKIE_NAME` | `jx_session` | Cookie name | CE |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` behind HTTPS in production | CE |
| `SESSION_COOKIE_HTTPONLY` | `true` (compose default) | Block JS access | CE |
| `SESSION_COOKIE_SAMESITE` | `lax` | CSRF protection | CE |
| `SESSION_COOKIE_DOMAIN` | (empty) | Cookie domain | CE |

## Database and Redis

| Variable | Default | Description | Edition |
|---|---|---|---|
| `DATABASE_URL` | assembled by compose as `postgresql://hugagent_user:${DB_PASSWORD}@postgres:5432/hugagent`; code fallback `sqlite:///./hugagent.db` | Primary DB connection string | CE |
| `DB_PASSWORD` | `hugagent_dev_password` (compose default) | PostgreSQL password (change in production) | CE |
| `SQLITE_FALLBACK_URL` | `sqlite:///./hugagent_dev.db` (code default) | SQLite fallback DB | CE |
| `DB_ECHO` | `false` | SQLAlchemy SQL echo | CE |
| `DB_POOL_SIZE` | `.env.example` 50; code default 20 | Connection-pool size | CE |
| `DB_POOL_MAX_OVERFLOW` | `10` | Pool overflow limit | CE |
| `DB_POOL_TIMEOUT` | `30` | Connection acquire timeout (s) | CE |
| `REDIS_URL` | `redis://redis:6379/0` (compose default) | Redis connection string | CE |
| `REDIS_SOCKET_TIMEOUT` | `30` (code default) | Socket read timeout (s); must exceed the streaming XREAD BLOCK 5 s | CE |

## Storage

| Variable | Default | Description | Edition |
|---|---|---|---|
| `STORAGE_TYPE` | `local` | `local` / `s3` / `oss` | CE (s3 / oss: EE) |
| `STORAGE_PATH` | `./storage` (fixed at `/app/storage` in containers) | Local storage root | CE |
| `S3_BUCKET` / `S3_REGION` / `S3_ENDPOINT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | (empty) / `us-east-1` / … | S3 or compatible service | EE |
| `S3_CDN_DOMAIN` | (empty) | CDN-accelerated domain | EE |
| `S3_PRESIGNED_URL_EXPIRY` | `900` | Presigned URL lifetime (s) | EE |
| `OSS_ENDPOINT` / `OSS_BUCKET` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_KEY_PREFIX` | (empty) | Alibaba Cloud OSS | EE |
| `OSS_PRESIGNED_URL_EXPIRY` | `900` | OSS presigned URL lifetime (s) | EE |
| `PROJECT_FILE_CAPACITY_BYTES` | `209715200` (200 MB) | Total upload-type file cap per project | CE |

## Knowledge base and file parsing

| Variable | Default | Description | Edition |
|---|---|---|---|
| `KNOWLEDGE_BASE` | (empty) | Set `dify` to inject Dify datasets into the capability center | EE (Dify integration) |
| `DIFY_URL` / `DIFY_API_KEY` | `http://your-dify-host:3001/v1` / … | Dify knowledge-base API | EE |
| `DIFY_ALLOWED_DATASET_IDS` | (empty = all) | Expose only the listed datasets (comma / newline / semicolon separated) | EE |
| `KB_DETAIL_CONTENT_MAX_CHARS` | `50000` (code default) | KB detail content truncation limit | CE |
| `MILVUS_URL` | `http://milvus:19530` | Vector DB shared by self-hosted KB and memory | CE |
| `MILVUS_TOKEN` | (empty) | Milvus auth token | CE |
| `RERANKER_URL` / `RERANKER_API_KEY` | (sample values) | Reranker service (OpenAI-compatible) | CE |
| `RERANKER_MODEL` | (empty = disabled) | Set a model name to enable retrieval reranking | CE |
| `FILE_PARSER_API_URL` | (empty) | External file-parsing (OCR / layout) service | CE |
| `FILE_PARSER_TIMEOUT` | `60` | Parsing timeout (s) | CE |
| `FILE_PARSER_LANG_LIST` | `ch` | OCR languages | CE |
| `FILE_PARSER_BACKEND` / `FILE_PARSER_PARSE_METHOD` | `pipeline` / `auto` | Parser backend and method | CE |
| `FILE_PARSER_FORMULA_ENABLE` / `FILE_PARSER_TABLE_ENABLE` | `true` / `true` | Formula / table parsing toggles | CE |

## MCP tools

| Variable | Default | Description | Edition |
|---|---|---|---|
| `MCP_HOST` | `mcp` (compose default) | MCP container hostname; set `127.0.0.1` for local debugging | CE |
| `INTERNET_SEARCH_ENGINE` | `tavily` (compose default) | Internet search engine | CE |
| `TAVILY_API_KEY` | (**required for internet search**) | Tavily Search API key | CE |
| `BAIDU_API_KEY` | (empty) | Baidu search API key | CE |
| `INTERNET_SEARCH_CN_ONLY` / `INTERNET_SEARCH_CN_STRICT` / `INTERNET_SEARCH_COUNTRY` / `INTERNET_SEARCH_AUTO_PARAMETERS` | (empty) | Regional / parameter tuning | CE |
| `QUERY_DATABASE_URL` | `http://your-database-api-host:6200` | HTTP backend for the data-warehouse query tool | EE (industry tool) |
| `QUERY_DATABASE_TIMEOUT_SECONDS` / `QUERY_DATABASE_RETRY_TIMES` / `QUERY_DATABASE_MAX_OUTPUT_TOKENS` | (empty) | Data-warehouse call parameters | EE |
| `INDUSTRY_URL` / `INDUSTRY_AUTH_TOKEN` | (sample values) | Industry-chain information API | EE (industry tool) |
| `COMPANY_API_URL` / `COMPANY_AUTH_TOKEN` | (empty) | Company-profile API | EE (industry tool) |
| `BACKEND_INTERNAL_URL` | `http://backend:3001` | Internal address for MCP servers (e.g. batch_runner) to call back into the backend | CE |
| `BACKEND_INTERNAL_TOKEN` | (**required for batch execution**) | MCP → backend callback token | CE |
| `HUGAGENT_USER_SKILLS_DIR` / `HUGAGENT_PROJECT_SKILLS_DIR` | `~/.hugagent/skills` / `.hugagent/skills` | Skill directory overrides | CE |
| `HUGAGENT_DISABLE_USER_SKILLS` / `HUGAGENT_DISABLE_PROJECT_SKILLS` | `0` | Disable user / project skills | CE |

## Sandbox

### Common

| Variable | Default | Description | Edition |
|---|---|---|---|
| `SANDBOX_PROVIDER` | `script_runner` | `script_runner` / `opensandbox` / `cube` | CE (persistent sandboxes opensandbox/cube: EE) |
| `SANDBOX_TOOLS_ENABLED` | `false` | Register the `bash` / `sandbox_put_artifact` / `sandbox_get_artifact` tools on the agent | CE |
| `SANDBOX_MAX_CONCURRENT` | `4` | Concurrent sandbox executions per backend process (reserved) | CE |
| `SANDBOX_RUNNER_URL` | `http://hugagent-script-runner:8900` | script_runner sidecar address | CE |
| `SANDBOX_TOOLS_TIMEOUT` / `SANDBOX_TOOLS_MAX_TIMEOUT` | `30` / `120` | Default / maximum timeout per bash command (s) | CE |
| `SANDBOX_TOOLS_MAX_MEMORY` | `256` | script_runner memory cap (MB) | CE |
| `MYSPACE_WRITE_CONFIRM` | `true` (code default) | Sandbox writes to `/myspace` require out-of-band user confirmation | CE |

### OpenSandbox (persistent sandbox, Enterprise Edition)

| Variable | Default | Description |
|---|---|---|
| `OPENSANDBOX_DOMAIN` | `http://opensandbox:8080` | OpenSandbox server address |
| `OPENSANDBOX_API_KEY` | (empty = insecure mode) | Must be a strong random string in production |
| `OPENSANDBOX_IMAGE` | `hugagent-opensandbox-custom:latest` (compose default; code fallback `opensandbox/code-interpreter:v1.0.2`) | Sandbox runtime image; the custom image pre-installs all project dependencies (`docker/Dockerfile.opensandbox`) |
| `OPENSANDBOX_DEFAULT_TIMEOUT_S` | `1800` | Sandbox TTL (s); destroyed when expired without renewal |
| `OPENSANDBOX_READY_TIMEOUT_S` | `90` | Maximum wait for sandbox readiness (s) |
| `OPENSANDBOX_REQUEST_TIMEOUT_S` | `120` | Per-HTTP-call timeout (s) |
| `OPENSANDBOX_PORT` | `8910` | Host debug port mapping (fixed 8080 in-container) |
| `OPENSANDBOX_POOL_JUPYTER_MIN_IDLE` / `MAX_IDLE` | `.env.example` 1/3; compose defaults 2/3 | Jupyter warm pool (persistent-session bucket) |
| `OPENSANDBOX_POOL_LIGHT_MIN_IDLE` / `MAX_IDLE` | `2` / `5` | Light bucket (one-shot executions) |
| `OPENSANDBOX_POOL_MAX_TOTAL` | `20` | Pool ceiling (idle + in use) |
| `OPENSANDBOX_IDLE_REAP_S` | `600` (compose default) | Idle reap threshold for persistent sessions (s); `<=0` disables |
| `OPENSANDBOX_SNAPSHOT_ENABLED` | `true` | Snapshot persistence master switch (snapshot+kill when idle, restore on reconnect) |
| `OPENSANDBOX_IDLE_SNAPSHOT_THRESHOLD_S` | `1500` | Idle beyond this triggers background snapshot + kill (must be below the sandbox TTL) |
| `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS` | `7` | Snapshot retention (GC sweeps expired rows) |
| `OPENSANDBOX_SNAPSHOT_WAIT_TIMEOUT_S` | `120` | Maximum poll for snapshot readiness (s) |
| `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED` | `true` | Direct myspace mount: binds the backend `myspace_cache/{uid}` into the sandbox at `/workspace/myspace/{uid}`, avoiding HTTP PUT sync; `false` falls back to the full-PUT path |

### CubeSandbox (E2B-compatible MicroVM, Enterprise Edition)

| Variable | Default | Description |
|---|---|---|
| `CUBE_NODE_IP` | (must set) | Cube node IP (the cube-dns sidecar resolves `*.cube.app` to it) |
| `CUBE_API_URL` | `http://<node-ip>:38473` | Control-plane REST address |
| `CUBE_API_KEY` | (empty) | Control-plane auth (CubeSandbox default `e2b_000000`) |
| `CUBE_API_SANDBOX_DOMAIN` | `cube.app:38573` | Data-plane sandbox domain (port allowed) |
| `CUBE_TEMPLATE` | (**required**) | Sandbox template id |
| `CUBE_DEFAULT_TIMEOUT_S` / `CUBE_REQUEST_TIMEOUT_S` | `1800` / `120` | Sandbox TTL / per-request timeout (s) |
| `CUBE_CA_BUNDLE` | (empty) | mkcert rootCA path inside the container (injected as `SSL_CERT_FILE`) |
| `CUBE_IDLE_REAP_S` | `600` | Idle reap threshold (s); `<=0` disables |
| `CUBE_POOL_MIN_IDLE` | `2` | Warm-pool idle target; `<=0` disables |
| `CUBE_OWNER_TAG` | (empty = orphan sweep off) | Environment owner tag; must be unique per environment when sharing one cube node |
| `CUBE_SKILL_PREPUSH` / `CUBE_SKILL_PREPUSH_MAX_MB` / `CUBE_SKILL_PREPUSH_CONCURRENCY` | `true` / `20` / `3` | Skill tar-packaging prepush optimization |
| `CUBE_NODE_SSH_HOST` / `PORT` / `USER` / `KEY` | falls back to `CUBE_NODE_IP` / `22` / `root` / `/home/appuser/.ssh/cube_node_key` | SSH config for the admin "apply dependencies" remote template rebuild |
| `CUBE_NODE_SSH_KEY_HOST` | `/home/<user>/.ssh/id_rsa` | Host private-key path (mounted read-only by compose) |
| `CUBE_BUILD_CTX_DIR` / `CUBE_BUILD_IMAGE_TAG` / `CUBE_BUILD_REGISTRY` | `/opt/cube-build` / `hugagent-cube-sandbox:latest` / `127.0.0.1:5000` | Node build context / image tag / local registry |
| `CUBE_BUILD_WRITABLE_LAYER` / `CUBE_BUILD_CPU` / `CUBE_BUILD_MEMORY` | `8Gi` / `2000` / `4000` | create-from-image resource parameters |
| `CUBE_BUILD_EXPOSE_PORTS` / `CUBE_BUILD_PROBE_PORT` / `CUBE_BUILD_PROBE_PATH` | `49983,49999` / `49999` / `/health` | Template ports and probe |
| `CUBE_BUILD_TIMEOUT_S` / `CUBE_BUILD_REGISTER_TIMEOUT_S` | `1800` / `900` | Build / register timeouts (s) |

## mem0 memory system

| Variable | Default | Description | Edition |
|---|---|---|---|
| `MEM0_ENABLED` | `false` | Master switch; when `false` all memory paths short-circuit with zero overhead | CE |
| `MEM0_GRAPH_ENABLED` | `false` | L3 graph memory (requires Neo4j) | CE |
| `MEM0_EMBED_URL` / `MEM0_EMBED_API_KEY` | (must set) | Embedding service (OpenAI-compatible) | CE |
| `MEM0_EMBED_MODEL` | `qwen3_embedding_8b` | Embedding model name | CE |
| `MEM0_EMBED_DIMS` | `.env.example` 1024; compose default 4096 | Vector dimensions (must match the model) | CE |
| `MEMORY_MODEL_URL` / `MEMORY_API_KEY` / `MEMORY_MODEL_NAME` | fall back to the primary model | Dedicated extraction LLM (uses the primary model when unset) | CE |
| `MILVUS_URL` / `MILVUS_TOKEN` | `http://milvus:19530` / (empty) | Vector DB | CE |
| `NEO4J_URL` | `bolt://neo4j:7687` | Neo4j address | CE |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` | `neo4j` / `hugagent_neo4j_2026` (compose default) | Neo4j credentials | CE |
| `MEMORY_LAYERED_ENABLED` | `true` | Layered memory (L1 Profile / L2 Fact / L3 Graph); `false` reverts to flat mem0 | CE |
| `MEMORY_AUDIT_ENABLED` | `true` | Memory audit-table writes (compliance trail) | EE |
| `MEMORY_RETRIEVAL_BUDGET_MS` | `600` | Fact vector-retrieval budget (ms); on timeout only the Profile is injected | CE |
| `MEMORY_BG_MAX_CONCURRENCY` | `8` | Background extraction / save concurrency cap | CE |
| `MEMORY_EXTRACT_TIMEOUT_S` | `30` | Per-extraction LLM call timeout (s) | CE |
| `MEMORY_PROFILE_MAX_CHARS` | `1500` | L1 Profile character cap (compression beyond) | CE |
| `MEMORY_FACT_DEFAULT_TTL_DAYS` | `180` | L2 Fact default TTL (days) | CE |
| `MEMORY_FROZEN_TOPK` | `5` | Fact top-K injected into the frozen block | CE |
| `MEMORY_BREAKER_THRESHOLD` / `MEMORY_BREAKER_COOLDOWN_S` | `3` / `60` | Milvus circuit-breaker threshold / cooldown (s) | CE |

## Edition, branding, and license

| Variable | Default | Description | Edition |
|---|---|---|---|
| `JX_EDITION` | `ee` (main repo default; `ce` in the CE-derived tree) | Edition facade: `ce` / `ee` | — |
| `BRAND_PRODUCT_NAME` | deployment brand in `.env.example`; code fallback `智能体平台` | Product display name | CE (changeable) |
| `BRAND_ORG_NAME` | (sample value) | Organization name | CE (changeable) |
| `BRAND_POWERED_BY` | `true` (code default) | "Powered by" attribution; removing it requires a commercial license | EE |
| `LICENSE_KEY_PATH` | (empty) | License file path inside the container (Ed25519-signed, verified offline in-process); empty + non-enforcing = internal deployment with full features | EE |
| `JX_LICENSE_REQUIRED` | `false` | `true` = private-delivery mode: without a valid license all EE capability bits are off | EE |
| `LICENSE_GRACE_DAYS` | `14` | Post-expiry grace period (days); features stay on, probes alert | EE |
| `LICENSE_PUBLIC_KEY` | (empty = built-in key) | Verification public-key override (key rotation) | EE |

See the [License mechanism](../editions/license.md) for details.

## Miscellaneous (rate limiting, circuit breakers, security, alerting)

| Variable | Default | Description | Edition |
|---|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | API rate limiting | CE |
| `RATE_LIMIT_STORAGE` | `memory://` | Rate-limit counter store (`redis://...` supported) | CE |
| `RATE_LIMIT_GLOBAL` / `RATE_LIMIT_PER_USER` | `500/minute` / `50/minute` | Global / per-user limits | CE |
| `CB_USER_CENTER_THRESHOLD` / `CB_USER_CENTER_TIMEOUT` | `5` / `60` | User-center circuit breaker | EE |
| `CB_MODEL_API_THRESHOLD` / `CB_MODEL_API_TIMEOUT` | `10` / `30` | Model-API circuit breaker | CE |
| `CB_STORAGE_THRESHOLD` / `CB_STORAGE_TIMEOUT` | `5` / `60` | Storage circuit breaker | CE |
| `CORS_ORIGINS` | compose default `http://localhost:3000,http://localhost:5173` | Allowed cross-origin sources | CE |
| `ENABLE_SECURITY_HEADERS` | (commented; recommended `true` in production) | Security response headers | CE |
| `AUDIT_LOG_RETENTION_DAYS` / `AUDIT_LOG_EXPORT_ENABLED` | `90` / `true` (commented samples) | Audit-log retention / export | EE |
| `ENABLE_LOG_MASKING` | (commented; recommended `true`) | Sensitive-data masking in logs | CE |
| `ALERT_EMAIL_TO` / `ALERT_EMAIL_FROM` / `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | (commented) | Alert email | CE |
| `PROMPT_PROVIDER` / `PROMPT_DIR` / `PROMPT_INLINE_TEMPLATE` / `JX_PROMPT_CONFIG` | `filesystem` / (empty) | Prompt-source overrides (default: DB first, filesystem fallback) | CE |

## Related source

| Feature | Files |
|---|---|
| Variable samples (deployment starting point) | `.env.example` |
| Centralized settings (single read point) | `src/backend/core/config/settings.py` |
| Compose-level injection and defaults | `docker-compose.yml`, `docker-compose.cube.yml` |
| Sandbox provider selection | `src/backend/core/sandbox/`, `settings.py::SandboxSettings` |
| Memory settings | `settings.py::MemorySettings`, `src/backend/core/memory/` (service.py / pipeline.py) |
| License / edition facade | `settings.py::LicenseSettings / EditionSettings` |
