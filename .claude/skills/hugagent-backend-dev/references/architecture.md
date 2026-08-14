# 后端架构参考

## 请求流转

```
Browser → Nginx (:3000 /api/ proxy) → FastAPI (api/app.py)
  → api/middleware/ (CORS → Logging → Error Handler → license_gate)
  → api/routes/v1/*.py (路由层；CE 注册表 api/routes/v1/__init__.py，EE 注册表 edition_ee/routes/registry.py)
    → core/services/*.py (服务层 — 业务逻辑)
      → core/db/repository/ (仓库层 — 数据访问，按领域分文件)
        → core/db/models/ (ORM 包 → PostgreSQL/SQLite)
```

## 分层职责

```
┌─────────────────────────────────────────┐
│ Routes (api/routes/v1/)                 │ ← HTTP 入口、参数校验、响应包装
│   - 依赖注入: auth, db                   │
│   - Pydantic 请求模型                    │
│   - _to_dict() 转换函数                  │
│   - 调用 Service                         │
├─────────────────────────────────────────┤
│ Services (core/services/)               │ ← 业务逻辑、权限校验、审计
│   - 构造函数接收 db: Session              │
│   - 内部创建 Repository                  │
│   - 幂等操作 (ensure_xxx)                │
│   - 抛 AppException                      │
├─────────────────────────────────────────┤
│ Repository (core/db/repository/)        │ ← 数据访问抽象（包，10 个领域文件）
│   - CRUD 操作                            │
│   - 分页查询                             │
│   - 软删除过滤                           │
│   - 不含业务逻辑                         │
├─────────────────────────────────────────┤
│ Models (core/db/models/)                │ ← ORM 定义（包，17 个领域文件）
│   - SQLAlchemy declarative              │
│   - 索引、约束、关系                     │
│   - EE 专属表登记 edition_ee/db/edition_tables.py │
│   - Alembic 迁移                        │
└─────────────────────────────────────────┘
```

## 聊天流式请求流转

```
POST /v1/chats/stream
  → api/routes/v1/chats.py
  → orchestration/chat_run_executor.py     # ChatRun + Redis Stream（后台 run，SSE 跟随，断线续播）
  → orchestration/workflow.py              # 流式编排主入口
    → core/services/chat_mode_service.py   # 解析对话模式 ChatModeSpec（standard/turbo/市场模式；装配契约）
    → orchestration/memory_integration.py  # 检索分层记忆并注入（user-role 冻结块，600ms 预算）
    → core/llm/agent_factory.py            # 构建 AgentScope 2.0 ReActAgent（按 ChatModeSpec 收窄工具面）
      → core/llm/mcp_manager.py            # MCP 客户端池（streamable-http → mcp 容器）
      → core/config/mcp_config.py          # MCP server 定义（端口真源 mcp_servers/_ports.py）
      → core/llm/middlewares.py            # AS2 中间件（动态模型、文件上下文、CitationAnchorMiddleware、OntologyGateMiddleware）
      → prompts/prompt_runtime.py          # 装配系统提示词（DB 版本池优先，prompt_text/ 兜底）
    → orchestration/streaming.py           # StreamingAgent：把 AS2 事件流映射为内部块事件
    → orchestration/citation_anchor.py     # 引用锚点唯一发号（AnchorAllocator，e1/e2/…，经中间件回注工具结果）
    → core/memory/service.py               # 流结束后后台保存记忆（L1/L2/L3）
    → orchestration/followups.py           # 流结束后后台生成追问建议（写入消息 extra_data）
```

> `orchestration/strategy.py` 仍在但已退化为占位符（两个分支都返回 MainOnlyStrategy），不再是主链路环节。

**平级编排入口**（不走普通 chat run 的独立驱动）：

| 入口 | 文件 | 说明 |
|------|------|------|
| 自主循环 | `orchestration/autonomous_loop.py` + `loop_planner.py` / `loop_evaluator.py` + `subagents/loop_reviewer.py` | driver 持有需求台账 feature_list.json，逐条注入 + 只读 reviewer 验收；路由 `v1/loops.py` |
| 批量执行 | `orchestration/batch_orchestrator.py` | batch_confirm 确认后逐行执行 |
| 计划模式 | `orchestration/subagents/plan_mode.py` | plan_generate / plan_execute run |
| 渠道入站 | `core/channels/`（protocol/registry/manager/inbound/outbound） | IM 消息归一为 InboundMsg 后复用聊天编排，回复经 markdown.py 降级回发 |
| 定时任务 | `orchestration/schedulers/`（automation / distillation_cron / evolution / memory_ttl） | croniter 调度 |

SSE 事件（前端解析真源 `src/frontend/src/hooks/chatStream.ts`）：

- 生命周期：`run_started`（首帧，携带续播 run_id）、`meta`、`error`、`end` / `data: [DONE]`（终止）、心跳为 SSE 注释行
- 正文/思考：`content`（delta 增量；别名 ai_message/text/delta）、`content_replace`、`thinking`、`compaction_notice`、`steer_applied`
- 工具：`tool_call_start`、`tool_call_delta`（参数流式）、`tool_call`、`tool_result`、`tool_pending`、`model_progress`（活性信号）
- 交互确认：`batch_confirm`、`file_confirm`、`design_pick`
- 计划/子智能体：`plan_update`、`subagent_event`
- 本体治理：`ontology_activation` / `ontology_gate` / `ontology_review` / `ontology_repair` / `ontology_revision` / `ontology_revision_thinking`

断线续播：`GET /v1/chats/stream/{run_id}`。追问建议（follow_up_questions）流结束后后台生成，也可能以 `follow_up` 事件流内直送。

## 模块索引

| 模块 | 路径 | 职责 |
|------|------|------|
| App 入口 | `api/app.py` | FastAPI 实例、中间件；路由按注册表自动注册 |
| CE 路由注册表 | `api/routes/v1/__init__.py` | **CE_ROUTERS 单一真源**（二元组）；re-export EE_ROUTERS |
| EE 路由注册表 | `edition_ee/routes/registry.py` | **EE_ROUTERS 单一真源**（三元组，带 license 能力位） |
| 依赖注入 | `api/deps.py` | require_admin（ADMIN_TOKEN）、require_config（CONFIG_TOKEN）、require_admin_or_config、require_system_settings、require_super_admin |
| 用户认证 | `core/auth/backend.py` | get_current_user / UserContext（桌面桥接 → session → API-Key → mock/remote） |
| 健康检查 | `api/health.py` | /health, /ready, /live |
| Schema | `api/schemas.py` | 请求/响应 Pydantic 模型 |
| 路由 | `api/routes/v1/` | 66 个路由文件 |
| 技能引擎 | `core/agent_skills/` | SKILL.md 解析、多源加载、{dir} 沙箱路径注入 |
| 生成物 | `core/artifacts/` | 注册与下载（store.py，local/oss 双模） |
| 认证 | `core/auth/` | backend.py, session.py, sso.py, permissions_iface.py（CE/EE 接缝） |
| 渠道 | `core/channels/` | 入站机器人框架：钉钉/飞书/企微/微信 adapter；加渠道 = 写 adapter + registry.py 注册 |
| 聊天 | `core/chat/` | context.py, tool_log.py |
| 配置 | `core/config/` | settings.py + catalog 五件套（catalog.json/catalog.py/loader/resolver/migration）+ mcp_config.py |
| 内容 | `core/content/` | 内容块、file_parser.py |
| 数据库 | `core/db/` | engine.py, models/（17 领域文件）, repository/（10 领域文件）, model_extensions.py, edition_tables.py（垫片） |
| 进化 | `core/evolution/` | GCE 共享契约（Episode/候选/结算）；控制面在 edition_ee/evolution |
| 基础设施 | `core/infra/` | exceptions, responses, logging, metrics, 限流, Redis, crypto |
| 知识库 | `core/kb/` | 分块、向量化、混合检索 + wiki/（LLM-Wiki 管线）+ wiki_router.py + external_provider.py（外接接缝） |
| License | `core/licensing/` | features.py（能力位+402）、manager.py（状态机） |
| LLM | `core/llm/` | agent_factory（含 turbo 参数）, chat_models, middlewares, mcp_manager/mcp_pool, offloader, subagent_tool, tools/ |
| 记忆 | `core/memory/` | service.py（mem0）、pipeline.py、profile.py（L1）、sanitizer.py、audit.py |
| 本体 | `core/ontology/` | Domain Pack 校验、运行时选取、工具闸（schemas/validator/revision/toolkit） |
| 沙箱 | `core/sandbox/` | protocol.py + script_runner / opensandbox / cube 三实现 |
| 服务 | `core/services/` | 70+ 业务服务（user/chat/chat_mode/channel/loop/kb/plugin/prompt_version/marketplace…） |
| 存储 | `core/storage/` | protocol, local, s3, oss |
| 编排 | `orchestration/` | workflow, chat_run_executor, streaming, citation_anchor, autonomous_loop, followups, batch_orchestrator, schedulers/, subagents/ |
| 提示词 | `prompts/` | prompt_runtime + provider + prompt_text/{default,code_exec,distillation,plan_mode,subagents,turbo}/ |
| MCP 服务器 | `mcp_servers/` | 12 个 server 目录、11 个在役（端口 9100–9114，真源 _ports.py；9105/9109-9111 为退役保留位） |
| 技能资产 | `skill_bundles/` | default/（5 内置）+ marketplace/（40+ 可安装） |
| EE 树 | `edition_ee/` | EE 专属路由/模型/KB provider（dify/fastgpt/weknora）/evolution 控制面；CE 派生树物理删除 |

## 新路由注册步骤

1. 创建 `api/routes/v1/my_feature.py`（EE 专属则放 `edition_ee/routes/`），文件内定义 `router = APIRouter(prefix="/v1/xxx", tags=["Xxx"])`
2. 注册进对应注册表：
   - 通用功能 → `api/routes/v1/__init__.py` 的 `CE_ROUTERS` 加 `("my_feature", "router")`
   - 企业版功能 → `edition_ee/routes/registry.py` 的 `EE_ROUTERS` 加 `("edition_ee.routes.my_feature", "router", "<license能力位>")`（能力位 `None` 表示显式豁免 feature 守卫）
3. **不要**在 `api/app.py` 手工 `include_router()` —— app 启动时经 `iter_edition_routers()` 按表自动注册，模块缺失（CE 派生树物理删除了 EE 文件）会被静默跳过
