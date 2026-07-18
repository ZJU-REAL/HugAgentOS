# 后端架构参考

## 请求流转

```
Browser → Nginx (:3000 /api/ proxy) → FastAPI (api/app.py)
  → api/middleware/ (CORS → Logging → Error Handler)
  → api/routes/v1/*.py (路由层；注册表见 api/routes/v1/__init__.py)
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
│ Repository (core/db/repository/)        │ ← 数据访问抽象（包，按领域分文件）
│   - CRUD 操作                            │
│   - 分页查询                             │
│   - 软删除过滤                           │
│   - 不含业务逻辑                         │
├─────────────────────────────────────────┤
│ Models (core/db/models/)                │ ← ORM 定义（包，11 个领域文件）
│   - SQLAlchemy declarative              │
│   - 索引、约束、关系                     │
│   - EE 专属表登记 edition_tables.py      │
│   - Alembic 迁移                        │
└─────────────────────────────────────────┘
```

## 聊天流式请求流转

```
POST /v1/chats/stream
  → api/routes/v1/chats.py
  → orchestration/chat_run_executor.py     # ChatRun + Redis Stream（后台 run，SSE 跟随，断线续播）
  → orchestration/workflow.py              # 流式编排主入口
    → orchestration/memory_integration.py  # 检索分层记忆并注入（user-role 冻结块，600ms 预算）
    → orchestration/strategy.py            # 路由策略（ROUTER_STRATEGY，默认 main_only）
    → core/llm/agent_factory.py            # 构建 AgentScope 2.0 ReActAgent
      → core/llm/mcp_manager.py            # MCP 客户端池（streamable-http → mcp 容器）
      → core/config/mcp_config.py          # MCP server 定义
      → core/llm/middlewares.py            # AS2 中间件（动态模型、文件上下文等）
      → prompts/prompt_runtime.py          # 装配系统提示词（DB 版本池优先，prompt_text/ 兜底）
    → orchestration/citations.py           # 提取 [ref:tool-N] 引用标记
    → core/memory/service.py               # 流结束后后台保存记忆（L1/L2/L3）
```

SSE 事件：`run_started`（首帧，携带续播 run_id）、`content`（delta 增量）、`thinking`、
`tool_call`、`tool_result`、`tool_pending`、`batch_confirm`、`file_confirm`、`meta`、`error`；
流以 `data: [DONE]` 终止，心跳为 SSE 注释行（15s）。**没有 `text` / `done` 事件。**
断线续播：`GET /v1/chats/stream/{run_id}`。

## 模块索引

| 模块 | 路径 | 职责 |
|------|------|------|
| App 入口 | `api/app.py` | FastAPI 实例、中间件；路由按注册表自动注册 |
| 路由注册表 | `api/routes/v1/__init__.py` | **CE_ROUTERS / EE_ROUTERS 单一真源**（EE 项带 license 能力位） |
| 依赖注入 | `api/deps.py` | require_admin（ADMIN_TOKEN）、require_admin_or_config、require_super_admin、team 权限 |
| 用户认证 | `core/auth/backend.py` | get_current_user / UserContext（AUTH_MODE: mock/session/remote） |
| 健康检查 | `api/health.py` | /health, /ready, /live |
| Schema | `api/schemas.py` | 请求/响应 Pydantic 模型 |
| 中间件 | `api/middleware/` | CORS, logging, error_handler |
| 路由 | `api/routes/v1/` | 50+ 路由文件 |
| 技能引擎 | `core/agent_skills/` | SKILL.md 解析、多源加载、{dir} 沙箱路径注入 |
| 生成物 | `core/artifacts/` | 注册与下载（store.py，local/oss 双模） |
| 认证 | `core/auth/` | backend.py, session.py, sso.py, permissions_iface.py（CE/EE 接缝） |
| 聊天 | `core/chat/` | context.py, tool_log.py |
| 配置 | `core/config/` | settings.py + catalog 五件套（catalog.json/catalog.py/loader/resolver/migration）+ mcp_config.py |
| 内容 | `core/content/` | 内容块、file_parser.py |
| 数据库 | `core/db/` | engine.py, models/（包）, repository/（包）, edition_tables.py |
| 基础设施 | `core/infra/` | exceptions, responses, logging, metrics, 限流, Redis |
| 知识库 | `core/kb/` | 分块、向量化、混合检索 |
| License | `core/licensing/` | features.py（能力位+402）、manager.py（状态机） |
| LLM | `core/llm/` | agent_factory, chat_models, middlewares, mcp_manager/mcp_pool, offloader, tools/ |
| 记忆 | `core/memory/` | service.py（mem0）、pipeline.py、profile.py（L1）、sanitizer.py |
| 沙箱 | `core/sandbox/` | protocol.py + provider 实现 |
| 服务 | `core/services/` | 30+ 业务服务（user/chat/catalog/kb/artifact/plan/automation/prompt_version/marketplace…） |
| 存储 | `core/storage/` | protocol, local, s3, oss |
| 编排 | `orchestration/` | workflow, chat_run_executor, strategy, citations, memory_integration, schedulers/, subagents/ |
| 提示词 | `prompts/` | prompt_runtime + provider + prompt_text/{default,code_exec,distillation,plan_mode}/ |
| MCP 服务器 | `mcp_servers/` | 8 个内置 server，streamable-http 常驻 mcp 容器（端口 9100–9107，真源 _ports.py） |
| 技能资产 | `skill_bundles/` | default/（内置）+ marketplace/（可安装） |

## 新路由注册步骤

1. 创建 `api/routes/v1/my_feature.py`，文件内定义 `router = APIRouter(prefix="/v1/xxx", tags=["Xxx"])`
2. 在 `api/routes/v1/__init__.py` 的注册表中加一项：
   - 通用功能 → `CE_ROUTERS` 加 `("my_feature", "router")`
   - 企业版功能 → `EE_ROUTERS` 加 `("my_feature", "router", "<license能力位>")`（能力位 `None` 表示显式豁免 feature 守卫）
3. **不要**在 `api/app.py` 手工 `include_router()` —— app 启动时按表自动注册，模块缺失（CE 派生树物理删除了 EE 文件）会被静默跳过
