# API 总览

> 最后更新：2026-07-19

HugAgentOS 后端是一个 FastAPI 应用，所有业务接口挂在 `/v1/*` 前缀下。生产部署中 Nginx 把 `/api/` 前缀剥掉后转发给后端（见 `src/frontend/default.conf.template`），因此**浏览器侧的完整路径是 `/api/v1/...`**，直接访问后端容器则是 `/v1/...`。本文示例统一使用本地开发地址 `http://localhost:3000/api`。

相关文档：[认证体系](../modules/auth.md) · [错误码参考](error-codes.md) · [环境变量](../deployment/environment-variables.md) · [License 与商业版](../editions/license.md)

## 统一响应信封

所有 `/v1/*` 接口（SSE 流式接口除外）都返回统一信封，由 `src/backend/core/infra/responses.py` 生成：

```json
{
  "code": 10000,
  "message": "Success",
  "data": { "chat_id": "abc123", "title": "新对话" },
  "trace_id": "req_1a2b3c4d5e6f7a8b",
  "timestamp": 1781136000000
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | int | 5 位业务码：`10000` 成功、`10001` 创建成功；错误码体系见 [错误码参考](error-codes.md) |
| `message` | string | 人类可读的结果描述 |
| `data` | any | 业务数据；错误时为附加上下文对象 |
| `trace_id` | string | 请求追踪 ID（`req_` + 16 位十六进制），用于日志排查 |
| `timestamp` | int | 毫秒级 UTC 时间戳 |

分页接口的 `data` 内嵌 `items` + `pagination`，由 `paginated_response()` 统一产出：

```json
{
  "code": 10000,
  "message": "Success",
  "data": {
    "items": [ { "chat_id": "chat_abc123", "title": "新对话" } ],
    "pagination": {
      "page": 1,
      "page_size": 20,
      "total_items": 42,
      "total_pages": 3,
      "has_previous": false,
      "has_next": true
    }
  },
  "trace_id": "req_...",
  "timestamp": 1781136000000
}
```

> 注意：FastAPI 依赖层直接抛出的 `HTTPException`（如 ADMIN_TOKEN 校验失败）返回的是 FastAPI 原生形如 `{"detail": ...}` 的响应，不走信封；前端对两种形态都做了兼容（`api.ts` 同时读取 `payload.data.login_url` 与 `payload.detail.data.login_url`）。

## 认证方式

后端有多套并行的鉴权机制，各自覆盖不同的接口面（实现见 `src/backend/api/deps.py` 与 `src/backend/core/auth/backend.py`）：

| 方式 | 凭据携带方法 | 适用范围 | 实现 |
|---|---|---|---|
| **会话 Cookie** | `Cookie: jx_session=<token>`（登录后自动种下） | 所有面向最终用户的 `/v1/*` 接口（chats、projects、kb、memories…） | `get_current_user`：Cookie → Redis 会话查找 |
| **个人 API-Key** | `Authorization: Bearer sk-jx-...` | 与会话 Cookie 等价的用户身份，适合脚本/第三方集成调用 | `sk-jx-` 前缀识别；明文仅创建时返回一次，DB 只存 SHA256（`core/services/api_key_service.py`）；在 `/v1/me/api-keys` 自助管理 |
| **Bearer token（mock/remote 模式）** | `Authorization: Bearer <token>` | `AUTH_MODE=mock`（开发：任意 token / 缺省 token 均放行为 mock 用户）或 `remote`（对接用户中心校验） | `get_current_user` 的兜底分支；`AUTH_MODE=session` 下不接受 |
| **ADMIN_TOKEN** | `Authorization: Bearer <ADMIN_TOKEN>` | `/admin` 管理台后端（`/v1/admin/skills`、`/v1/admin/kb` 等）及内容块写接口 | `require_admin`（环境变量 `ADMIN_TOKEN`） |
| **CONFIG_TOKEN** | `Authorization: Bearer <CONFIG_TOKEN>` | `/config` 系统配置台后端（`/v1/config/*`、`/v1/models`、`/v1/service-configs`、部分 `/v1/admin/*` 观测接口） | `require_config`（环境变量 `CONFIG_TOKEN`） |
| **ADMIN 或 CONFIG 任一** | 同上 | 提示词快照导入/导出（`/v1/content/prompts/export|import`） | `require_admin_or_config` |
| **super_admin 会话** | 会话 Cookie（用户 `extra_data.role == "super_admin"`），合法 ADMIN_TOKEN 可兜底 | 个别跨用户管理操作 | `require_super_admin` |
| **BACKEND_INTERNAL_TOKEN** | `Authorization: Bearer <token>` | 仅 `/v1/internal/batch/*`（服务间内部调用）；未配置该变量时接口直接 503 拒绝（fail-closed） | `api/routes/v1/internal_batch.py` |

鉴权优先级（`get_current_user`）：**Cookie 会话 → API-Key Bearer（`sk-jx-` 前缀）→ mock/remote Bearer**。携带了形似 API-Key 的 Bearer 但校验失败时直接 401，绝不静默降级为匿名。

token 校验失败会写入审计日志（`AuditLogRepository.log_denial`）；完全不带 header 的探测流量不入库，避免放大 `audit_logs` 表。

```bash
# 用户身份（API-Key）
curl http://localhost:3000/api/v1/chats \
  -H "Authorization: Bearer sk-jx-xxxxxxxx"

# 管理台身份（ADMIN_TOKEN）
curl http://localhost:3000/api/v1/admin/skills \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

## SSE 流式协议

流式聊天端点 `POST /v1/chats/stream`（`src/backend/api/routes/v1/chats.py`）校验通过后启动一个后台 run，再以 SSE 跟随该 run 实时下发事件；断线后可用 `GET /v1/chats/stream/{run_id}` 从任意偏移续播。事件由 `src/backend/orchestration/workflow.py` 产出、`src/backend/orchestration/chat_run_executor.py` 序列化上 wire。

**Wire 格式**：`Content-Type: text/event-stream`，每个事件一行 `data: {JSON}`，事件类型放在 JSON 的 `type` 字段里（不使用 SSE 的 `event:` 行）；流以 `data: [DONE]` 结束。静默超过 15 秒时发送 SSE 注释行 `: heartbeat` 维持反代连接。

### 请求

```bash
curl -N http://localhost:3000/api/v1/chats/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-jx-xxxxxxxx" \
  -d '{
    "chat_id": "chat_abc123",
    "message": "帮我查一下北京今天的天气",
    "chat_mode": "fast"
  }'
```

请求体为 `ChatRequest`（`src/backend/api/schemas.py`）：`chat_id`、`message` 必填；可选 `model_name`、`chat_mode`（`fast`/`medium`/`high`/`max`）、`attachments`、`enabled_kbs` / `enabled_skills` / `enabled_mcps` / `enabled_agents` 等。

### 事件类型

| `type` | 触发时机 | 主要负载字段 |
|---|---|---|
| `run_started` | 流的第一帧 | `run_id`（用于续播/取消）、`message_id`、`chat_id` |
| `thinking` | 推理/思考阶段 | `message`（阶段提示）或 `delta`（思考增量文本） |
| `content` | 正文文本增量 | `event: "ai_message"`、`delta`、`chat_id` |
| `tool_call` | Agent 发起工具调用 | `tool_name`、`tool_display_name`、`tool_args`、`tool_id`、`subagent_name?` |
| `tool_result` | 工具返回结果 | `tool_name`、`result`（JSON）、`tool_id`、`citations`（引用项列表） |
| `tool_pending` | 模型缓冲工具参数/调用启动间隙 | `reason`（如 `tool_call_start` / `llm_buffering`） |
| `file_confirm` | 工具挂起等待用户确认「我的空间」写操作 | `confirm_id`、`op`、`logical_path`、`message`、`expired`；流不结束，用户带外 `POST /v1/chats/{chat_id}/file-confirm` 后续跑 |
| `batch_confirm` | 批量执行计划等待用户确认 | `plan_id`、`total`、`preview`、`default_template`、`placeholder_keys`；确认走 `POST /v1/batch/{plan_id}/confirm` |
| `meta` | 回答结束的收尾帧 | `route`、`sources`、`artifacts`、`citations`、`warnings`、`is_markdown`、`message_id`、`workspace_files` |
| `error` | 流式异常 | `error`（用户可读消息）、`chat_id` |

### 事件样例

```text
data: {"type": "run_started", "run_id": "run_9f8e7d", "message_id": "msg_001", "chat_id": "chat_abc123"}

data: {"type": "thinking", "message": "正在分析您的问题...", "chat_id": "chat_abc123"}

data: {"type": "tool_call", "tool_name": "internet_search", "tool_display_name": "联网搜索", "tool_args": {"query": "北京 今天 天气"}, "tool_id": "call_01", "chat_id": "chat_abc123"}

data: {"type": "tool_result", "tool_name": "internet_search", "result": {"result": {"query": "北京 今天 天气"}}, "tool_id": "call_01", "citations": [{"id": "internet_search-1", "title": "..."}], "chat_id": "chat_abc123"}

data: {"type": "content", "event": "ai_message", "delta": "今天北京多云，", "chat_id": "chat_abc123"}

data: {"type": "meta", "route": "main", "sources": [], "artifacts": [], "citations": [...], "warnings": [], "is_markdown": true, "chat_id": "chat_abc123", "message_id": "msg_001", "workspace_files": []}

data: [DONE]
```

正文中的 `[ref:tool_name-N]` 引用标记由 `orchestration/citations.py` 解析为 `citations` 项，前端据此渲染角标（见 [对话模块](../modules/chat.md)）。

### 续播与取消

回答在后台 run 中执行，SSE 只是「跟随」——断开连接不会终止生成：

```bash
# 断线后从头续播（from_offset 可指定起始事件偏移）
curl -N "http://localhost:3000/api/v1/chats/stream/run_9f8e7d?from_offset=0" \
  -H "Authorization: Bearer sk-jx-xxxxxxxx"

# 主动取消生成
curl -X POST http://localhost:3000/api/v1/chat-runs/run_9f8e7d/cancel \
  -H "Authorization: Bearer sk-jx-xxxxxxxx"
```

续播校验 run 归属：非属主返回 403、run 不存在返回 404。`GET /v1/chats/{chat_id}/active-run` 可查询某会话当前是否有进行中的 run（前端刷新页面后据此重连）。run 静默超过 `CHAT_RUN_INACTIVITY_TIMEOUT_SEC`（默认 600 秒）会被判定为僵死并终止。

### 其他 SSE 端点

其余 SSE 端点复用同一 wire 格式：`GET /v1/batch/{plan_id}/stream`（批量执行进度）、计划模式相关流会额外出现 `plan_generated` / `plan_error` 等事件。非流式版本为 `POST /v1/chats/send`，一次性返回完整信封。

## 健康检查

`src/backend/api/health.py`，注册在根路径（经 Nginx 即 `/api/health` 等），无需鉴权：

| 端点 | 方法 | 用途 | 行为 |
|---|---|---|---|
| `/health` | GET | 负载均衡健康检查 | 仅确认进程存活，不查依赖；返回 `{status, service, timestamp}` |
| `/ready` | GET | K8s readiness 探针 | 逐项检查 database / storage / redis / user_center，任一失败返回 503 `{ready: false, checks: {...}}` |
| `/live` | GET | K8s liveness 探针 | 恒返回 `{alive: true}` |

## 路由清单

路由注册表的单一真源是 `src/backend/api/routes/v1/__init__.py`：`CE_ROUTERS` 元组列出社区版路由，`EE_ROUTERS` 列出商业版路由及其对应的 license 能力位。EE 路由在 CE 派生树中被物理删除（注册表静默跳过），在 EE 部署中由 license 能力位做第二道守卫——**未授权能力位的请求统一返回 HTTP 402 / code 40201**（机制详见 [License 说明](../editions/license.md)）。

鉴权列说明：「用户」= 会话 Cookie 或个人 API-Key（`get_current_user`）；「ADMIN」/「CONFIG」分别指对应 token 的 Bearer 头。

### 社区版（CE）路由

| 分组 | 模块（`api/routes/v1/`） | 前缀 | 代表端点 | 鉴权 |
|---|---|---|---|---|
| 会话与消息 | `chats.py` | `/v1/chats` | `POST /stream`（SSE）、`GET /stream/{run_id}`（续播）、`POST /send`（非流式）、`GET /`、`GET /{chat_id}/messages`、`POST /{chat_id}/share` | 用户 |
| 会话与消息 | `chat_runs.py` | `/v1/chat-runs` | `POST /{run_id}/cancel` | 用户 |
| 会话与消息 | `chat_shares.py` | `/v1/chat-shares` | `POST /`、`GET /{share_id}`、`POST /{share_id}/revoke` | 用户 |
| 会话与消息 | `summary.py` | `/v1/summary` | `POST /`（会话标题摘要） | 用户 |
| 会话与消息 | `classify.py` | `/v1/classify` | `POST /`（业务主题分类） | 用户 |
| 用户与偏好 | `users.py` | `/v1` | `GET/PATCH /me`、`POST /me/avatar`、`GET/PUT /users/{id}/preferences` | 用户 |
| 用户与偏好 | `me.py` | `/v1/me` | `GET /teams`、`GET /teams/{id}/members`、`GET /users/search` | 用户 |
| 用户与偏好 | `me_capabilities.py` | `/v1/me` | `POST /mcp-servers`、`POST /skills/upload`（个人自定义能力） | 用户 |
| 用户与偏好 | `api_keys.py` | `/v1/me/api-keys` | `GET / POST /`、`PATCH/DELETE /{key_id}` | 用户 |
| 个人空间 | `projects.py` | `/v1/projects` | `GET/POST /`、`POST /{id}/files/upload`、`GET /{id}/chats` | 用户 |
| 个人空间 | `myspace_folders.py` | `/v1/myspace/folders` | `GET/POST /`、`POST /move-artifact` | 用户 |
| 个人空间 | `artifacts.py` | `/v1/artifacts` | `GET /`、`GET /favorites`、`DELETE /{artifact_id}` | 用户 |
| 记忆 | `memories.py` | `/v1/memories` | `GET /`、`DELETE /{memory_id}`、`GET/PATCH /settings`、`GET /profile`、`GET /graph` | 用户 |
| 领域本体 | `ontologies.py` | `/v1/ontologies`、`/v1/admin/ontologies` | 用户设置/运行时预览；Domain Pack 版本、构建预检、门禁/评审证据、闭环指标和演进草案治理 | 用户 / ADMIN |
| 能力目录 | `catalog.py` | `/v1/catalog` | `GET /`（能力目录）、`PATCH /{kind}/{id}` | 用户 |
| 能力目录 | `kb.py` | `/v1/catalog/kb` | `POST /`、`POST /{kb_id}/documents`、`GET /{kb_id}/chunks`（个人知识库） | 用户 |
| 能力目录 | `marketplace.py` | `/v1/marketplace` | `GET /skills`、`POST /install`、`POST /submissions`（技能市场） | 用户 |
| 能力目录 | `agents.py` | `/v1/agents` | `GET/POST /`、`PUT/DELETE /{agent_id}`（个人智能体） | 用户 |
| 模型 | `models.py` | `/v1/models` | `GET /capabilities`（公开）；`GET/POST /providers`、`PUT /roles/{role_key}` 等管理端点 | 公开 / CONFIG |
| 文件 | `file_upload.py` | `/v1/file` | `POST /upload`、`PUT /{file_id}` | 用户 |
| 文件 | `file_parse.py` | `/v1/file` | `POST /parse`（文档解析） | 用户 |
| 内容块 | `content.py` | `/v1/content` | `GET /docs`（公开读）；`PUT /docs/{block_id}`（ADMIN）、`GET /prompts/export` / `POST /prompts/import`（ADMIN 或 CONFIG） | 公开 / ADMIN / CONFIG |
| 自动化 | `automations.py` | `/v1/automations` | `GET/POST /`、`POST /{task_id}/trigger`、`GET /notifications/list` | 用户 |
| 批量执行 | `batch.py` | `/v1/batch` | `GET /active`、`POST /{plan_id}/confirm`、`GET /{plan_id}/stream`（SSE） | 用户 |
| 批量执行 | `internal_batch.py` | `/v1/internal/batch` | `POST /resolve`（服务间内部接口） | BACKEND_INTERNAL_TOKEN |
| 计划模式 | `plans.py` | `/v1/plans` | `POST /generate`、`POST /{plan_id}/execute`、`POST /{plan_id}/cancel` | 用户 |
| 系统信息 | `config.py` | `/v1/config` | `GET /tool-names`（工具显示名映射，公开） | 公开 |
| 系统信息 | `meta.py` | `/v1/meta` | `GET /edition`（版本/模式/能力位布尔表，公开探针，绝不暴露 license 详情） | 公开 |
| —（仅 Schema） | `kb_models.py` | — | 无端点：知识库路由共用的 Pydantic 模型定义 | — |

### 商业版（EE）路由

第三列为 `EE_ROUTERS` 中声明的 license 能力位；`—` 表示显式豁免守卫（license 失效时也必须可达，否则陷入「402 → 登出 → 登录 → 402」死循环）。

| 分组 | 模块 | 前缀 | 代表端点 | 鉴权 | 能力位 |
|---|---|---|---|---|---|
| 审计 | `audit.py` | `/v1/audit` | `GET /logs`、`GET /logs/export/csv`、`GET /stats` | 用户 | `audit` |
| 内容管理台 | `admin_skills.py` | `/v1/admin/skills` | `GET/POST /`、`POST /upload`、`PUT /{skill_id}/toggle` | ADMIN | `content_admin` |
| 内容管理台 | `admin_kb.py` | `/v1/admin/kb` | `GET/POST /`、`POST /{kb_id}/documents`、`GET /{kb_id}/chunks` | ADMIN | `content_admin` |
| 内容管理台 | `admin_prompts.py` | `/v1/admin/prompts` | `GET /parts`、`GET/POST /versions`、`POST /preview` | CONFIG | `content_admin` |
| 内容管理台 | `admin_mcp_servers.py` | `/v1/admin/mcp-servers` | `GET/POST /`、`POST /{server_id}/test`、`POST /reload-pool` | CONFIG | `content_admin` |
| 内容管理台 | `admin_agents.py` | `/v1/admin/agents` | `GET/POST /`、`PUT /{agent_id}/toggle`、`GET /export` | ADMIN | `content_admin` |
| 内容管理台 | `admin_skill_drafts.py` | `/v1/admin/skill-drafts` | `GET /`、`POST /{draft_id}/approve`（技能蒸馏审核） | ADMIN | `content_admin` |
| 内容管理台 | `admin_sandbox.py` | `/v1/admin/sandbox` | `GET /deps`、`POST /rebuild`（沙盒依赖重建） | ADMIN | `content_admin` |
| 内容管理台 | `admin_marketplace.py` | `/v1/admin/marketplace` | `GET /submissions`、`POST /submissions/{id}/approve`（上架审核） | ADMIN | `content_admin` |
| 计费 | `admin_usage_logs.py` | `/v1/admin/usage-logs` | `GET /`、`GET /summary`、`GET /models` | CONFIG | `billing` |
| 计费 | `admin_billing.py` | `/v1/admin/billing` | `GET /summary`、`GET/POST /pricing` | CONFIG | `billing` |
| 观测与审计 | `admin_chat_history.py` | `/v1/admin/chat-history` | `GET /sessions`、`GET /export`（全量会话审查） | CONFIG | `audit` |
| 观测与审计 | `admin_logs.py` | `/v1/admin/logs` | `GET /tools`、`GET /subagents`、`GET /trace/{trace_id}` | CONFIG | `audit` |
| 登录与会话 | `auth.py` | `/v1/auth` | `POST /ticket/exchange`（SSO 票据换会话）、`GET /session/check`、`POST /logout` | 公开（会话基础设施） | — |
| 配置台 | `config_verify.py` | `/v1/config` | `GET /verify`（CONFIG_TOKEN 校验） | CONFIG | — |
| 配置台 | `config_license.py` | `/v1/config/license` | `GET /`（license 详情）、`POST /`（更换 license） | CONFIG | — |
| 多租户 | `config_users.py` | `/v1/config/users` | `GET /`、`PATCH /{user_id}/status`、`POST /{user_id}/reset-password` | CONFIG | `multi_tenancy` |
| 多租户 | `config_teams.py` | `/v1/config/teams` | `GET/POST /`、`POST /{team_id}/members` | CONFIG | `multi_tenancy` |
| 多租户 | `config_invites.py` | `/v1/config/invite-codes` | `GET/POST /`、`POST /{code}/revoke` | CONFIG | `multi_tenancy` |
| 多租户 | `team_files.py` | `/v1/my-teams`、`/v1/teams`、`/v1/artifacts` | `GET /my-teams`、`POST /teams/{id}/files/upload`、`POST /artifacts/{id}/move-to-team` | 用户 + 团队文件权限 | `multi_tenancy` |
| 系统配置 | `config_security.py` | `/v1/config/security` | `GET /sandbox/overview`、`GET /audit-logs`、`GET /system-health` | CONFIG | `system_config` |
| 系统配置 | `service_configs.py` | `/v1/service-configs` | `GET/PUT /`、`POST /test/{group_key}`（外部服务连通性测试） | CONFIG | `system_config` |

### `/v1` 之外的路由

| 模块 | 前缀 | 代表端点 | 鉴权 | 说明 |
|---|---|---|---|---|
| `api/health.py` | `/` | `GET /health`、`/ready`、`/live` | 公开 | 健康检查 |
| `api/routes/files.py` | `/files` | `GET /{file_id}`（下载）、`GET /{file_id}/preview` | 视文件归属 | 生成文件分发 |
| `api/routes/v1/mock_sso.py`（`login_router`） | `/` | `GET/POST /login`、`POST /register` | 公开 | 本地账号登录/注册（始终注册） |
| `api/routes/v1/mock_sso.py`（`mock_sso_router`） | `/mock-sso` | `GET/POST /login`、`POST /ticket/exchange` | 公开 | Mock SSO 页面，仅 `mock`/`local` 登录模式注册 |

## 版本与兼容性

- 当前只有 `v1` 一个 API 版本；无独立的 API 版本协商头。
- `GET /v1/meta/edition` 是判断部署形态（CE/EE、license 模式、能力位）的标准探针，前端启动时即调用。
- 所有时间戳为毫秒级 UTC；所有响应 JSON 使用 UTF-8 且不转义中文（`ensure_ascii=False`）。
