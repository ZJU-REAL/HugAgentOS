# 模型接入

> 最后更新：2026-06-11

HugAgentOS 通过 **OpenAI 兼容协议**接入任意大模型端点（vLLM、Ollama、DashScope、DeepSeek、各类网关均可）。模型配置以数据库为准——管理员在 Config 管理台登记「模型供应商」，再把供应商绑定到「角色」（主推理、摘要、向量化等），全链路经 `ModelConfigService` 30 秒 TTL 缓存生效，改配置无需重启。`MODEL_URL` / `API_KEY` / `BASE_MODEL_NAME` 等环境变量保留为兼容兜底与 MCP 子进程注入用途。

## 配置模型：供应商 + 角色

两张表（`core/db/models.py`）：`model_providers`（base_url / api_key / model_name / extra_config / is_active）与 `model_role_assignments`（role_key → provider_id）。角色定义在 `core/db/model_repository.py::ROLE_DEFINITIONS`：

| role_key | 用途 | 类型 |
|---|---|---|
| `main_agent` | 主智能体推理（必配，缺失时聊天接口直接 503） | chat |
| `summarizer` | 会话标题摘要 + 分类 | chat |
| `followup` | 追问问题生成 | chat |
| `memory` | 记忆抽取（mem0） | chat |
| `embedding` | 文本向量化（KB / 记忆） | embedding |
| `reranker` | 检索结果重排序 | reranker |
| `chart` | 图表代码生成 | chat |
| `plan_agent` | 计划模式推理（未配置降级 main_agent） | chat |
| `code_exec` | 代码执行推理（可选运维覆盖，默认不引用） | chat |

`extra_config` 支持 `temperature` / `max_tokens` / `timeout` / `context_length`（上下文窗口，供压缩阈值计算）/ `supports_reasoning_effort`（是否支持思考档位）等键。

### 管理 API（api/routes/v1/models.py，CONFIG_TOKEN）

| 端点 | 说明 |
|---|---|
| `GET/POST/PUT/DELETE /v1/models/providers...` | 供应商 CRUD；保存前做**真实连通性预校验**（按类型分别打 `/chat/completions`、`/embeddings`、`/rerank`），失败返回 400；base_url 自动规范为 `…/v1`；响应中 api_key 脱敏 |
| `POST /v1/models/providers/{id}/test`、`POST /v1/models/providers/test` | 已保存 / 未保存配置的连通性测试 |
| `GET /v1/models/roles`、`PUT/DELETE /v1/models/roles/{role_key}` | 角色分配（校验供应商类型与角色匹配；被引用的供应商禁删） |
| `GET /v1/models/export`、`POST /v1/models/import` | 模型配置跨环境迁移 |
| `GET /v1/models/capabilities` | **公开端点**：仅暴露 `main_agent.supports_reasoning_effort` 布尔，前端据此显示「思考·中/高/超高」档位 |

所有写操作后调 `ModelConfigService.invalidate_cache()`，30 秒内全进程生效。

## JxOpenAIChatModel（core/llm/chat_models.py）

运行时模型实例统一由 `make_chat_model()` 构造，返回 `JxOpenAIChatModel`——AgentScope 2.0 `OpenAIChatModel` 的子类，解决三件原生类做不到的事：

1. **流式读超时**：长 tool_call 参数生成时单 chunk 可静默 130–160 秒，注入自定义 `httpx.AsyncClient` 把 read 超时抬到 600 秒（`STREAM_READ_TIMEOUT_S`），connect/write/pool 仍用供应商配置的 timeout。
2. **思考链开关**：Qwen / MiniMax 等 OpenAI 兼容端点的思考开关走 `extra_body.chat_template_kwargs`（`enable_thinking` / `thinking` / `reasoning_effort`），每次调用注入。
3. **结构化输出兜底（L3）**：上下文压缩走 `generate_structured_output()`，个别模型返回 malformed JSON 会导致整轮 `reply()` 崩溃——子类捕获异常并返回 `L3_SYNTHETIC_METADATA` 占位摘要，压缩照常落盘、对话继续。

重试策略：模型层 `max_retries=0`，由 agent 层 `ModelConfig(max_retries=3)` 独占重试，避免两层重试倍乘。

## 动态模型切换（chat_mode）

前端每条消息可带 `chat_mode`（`fast / medium / high / max`），在 reply 开始前由 `core/llm/middlewares.py::DynamicModelMiddleware`（`on_reply`）热切 `agent.model`：

```
chat_mode → hooks._resolve_chat_mode(agent.state)
          → hooks._get_main_model(mode)        # 进程级实例缓存，随 ModelConfigService.version 失效
   fast   → disable_thinking=True
   medium → 思考开（supports_reasoning_effort 时带 effort=medium）
   high/max → reasoning_effort=high/max（端点须声明支持）
```

旧客户端只传 `enable_thinking` 时回退映射为 medium / fast。这就是 1.x「pre_reply 动态模型 hook」在 AgentScope 2.0 下的形态——hook 工厂已重构为中间件，`core/llm/hooks.py` 仅保留纯函数 helper。

## 环境变量与 MCP 子进程注入

| 变量 | 现状 |
|---|---|
| `MODEL_URL` / `API_KEY` / `BASE_MODEL_NAME` | 不再被主链路直接读取；保留在 `core/config/settings.py` 与个别旧路径（如 `internal_batch.py`、`kb_processing.py`）作兜底 |
| MCP 子进程 | `ModelConfigService.get_mcp_env_overlay()` 把 `main_agent` / `chart` / `embedding` / `reranker` 角色映射回 `MODEL_URL`、`OPENAI_API_KEY`、`MEM0_EMBED_*`、`RERANKER_*` 等旧变量名注入；常驻 MCP 容器内则用 `core/config/runtime_env.py::get_runtime_value()` 先查 DB 再回退 env，保证后台改模型对 MCP 即时生效 |

完整变量清单见 [环境变量](../deployment/environment-variables.md)。

## 个人 API-Key

用户可签发个人密钥以编程方式调用平台 API（`api/routes/v1/api_keys.py` + `core/services/api_key_service.py`）：

- 明文形如 `sk-jx-<random>`，**仅创建时返回一次**，DB 只存 SHA256 哈希 + 前缀；支持 7/30/90/180/365 天有效期或永不过期、启停与撤销。
- 调用方式 `Authorization: Bearer sk-jx-...`，鉴权层（`core/auth/backend.py`）识别 key 前缀后经 `resolve_api_key` 解析为用户上下文，校验启用/未撤销/未过期。
- 受权限位 `can_use_api_key` 门控（`users_shadow.metadata`）。社区版默认放开即可用；**由组织管理员按用户授权位统一管控为商业版（EE）**。

端点：`GET/POST /v1/me/api-keys`、`PATCH /v1/me/api-keys/{id}`（启停）、`DELETE /v1/me/api-keys/{id}`（撤销）。

## 计费与用量

token 用量在流式收尾的 `meta.usage` 中统计（`orchestration/streaming.py` 从 `ModelCallEndEvent` 累计）并随消息持久化，之上有两组管理台报表（`CONFIG_TOKEN`）：

- **用量日志** `api/routes/v1/admin_usage_logs.py`：`GET /v1/admin/usage-logs`（明细）、`/summary`（汇总）、`/models`（去重模型名）。
- **计费报表** `api/routes/v1/admin_billing.py`（**商业版 EE**——完整管理台能力）：`GET /v1/admin/billing/summary`（按用户/模型聚合成本）、模型定价 CRUD（`/pricing`，输入/输出单价、币种）、`GET /v1/admin/billing/export`（CSV 成本导出）。

社区版用户可查看自己的 token 用量；组织级计费汇总、定价管理与成本导出归商业版，配额管控在商业版规划中。

## 路由策略（ROUTER_STRATEGY）

`orchestration/strategy.py`：`ROUTER_STRATEGY=main_only`（默认）恒路由到主智能体；`llm_router` 为预留占位，当前实现同样回落 `MainOnlyStrategy`（safe-by-default）。实际的多智能体分流由 `@提及` + `call_subagent` 工具完成，见 [对话与智能体编排](chat.md)。

## 相关源码

| 主题 | 路径 |
|---|---|
| 模型工厂 / JxOpenAIChatModel | `src/backend/core/llm/chat_models.py` |
| 角色解析服务（DB + 缓存） | `src/backend/core/services/model_config.py` |
| 角色定义 / 供应商仓储 | `src/backend/core/db/model_repository.py` |
| 管理 API | `src/backend/api/routes/v1/models.py` |
| 动态模型中间件 | `src/backend/core/llm/middlewares.py::DynamicModelMiddleware`，helper 在 `core/llm/hooks.py` |
| MCP env 注入 | `src/backend/core/services/model_config.py::get_mcp_env_overlay`，`core/config/runtime_env.py` |
| 个人 API-Key | `src/backend/api/routes/v1/api_keys.py`，`core/services/api_key_service.py`，`core/auth/backend.py` |
| 用量日志 / 计费 | `src/backend/api/routes/v1/admin_usage_logs.py`，`api/routes/v1/admin_billing.py` |
| 路由策略 | `src/backend/orchestration/strategy.py` |
| 主模型缺失快速失败 | `src/backend/api/routes/v1/chats.py::_ensure_main_model_configured` |
