# 系统整体架构

> 最后更新：2026-10-23

HugAgentOS 是一个开源、可自托管的 Agent Harness，提供 HugAgentOS Cloud（全容器化服务端）与 HugAgentOS Local（桌面本机）两种运行形态。前端为 React 单页应用，后端为 FastAPI 异步服务，智能体底座为 AgentScope 2.0 的 ReActAgent，工具生态通过 MCP（Model Context Protocol）以独立进程提供，代码执行落在可插拔的沙箱驱动上。平台以「开源社区版（CE）+ 商业版（EE）」双形态发行，两版共用一套代码，通过路由注册表、建表边界与 license 能力位三道接缝裁剪（详见 [版本与授权](../editions/overview.md)）。

本文从五个视角描述系统：分层架构、本体驱动的企业可信控制平面、一次对话请求的完整生命周期、容器拓扑、关键设计决策。更深入的模块拆解见 [后端架构](./backend.md)、[前端架构](./frontend.md) 与 [数据模型](./data-model.md)。

## 技术栈一览

| 层 | 技术选型 |
|---|---|
| 前端 | React 19 + TypeScript + Vite + Ant Design + Zustand |
| 接入 | Nginx（托管静态资源，`/api` 反向代理到后端） |
| 后端 | Python + FastAPI + SQLAlchemy + Alembic |
| 智能体 | AgentScope 2.0（ReActAgent + Toolkit + 中间件） |
| 工具协议 | MCP（streamable-http，独立 `mcp` 容器常驻进程） |
| 沙箱 | OpenSandbox（CE / EE）、CubeSandbox（商业版 EE）、script-runner 轻量沙箱 |
| 数据 | PostgreSQL（生产）/ SQLite（开发）、Redis、Milvus + Neo4j（可选记忆组件） |
| 存储 | 本地磁盘 / S3 / 阿里云 OSS（云存储为商业版 EE） |

## 分层架构图

```
┌────────────────────────────────────────────────────────────────────┐
│  入口：Web（React SPA）· 桌面客户端（Tauri）· IM 渠道 · API · 定时 · 心跳 │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ HTTP / SSE（/api 前缀）
┌──────────────────────────────▼─────────────────────────────────────┐
│  FastAPI（src/backend/api/）                                        │
│  中间件：CORS · 结构化日志 · 统一错误处理 · 版本路由注册表           │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│  Run 层                                                             │
│  core/runs：持久 Run、数据库租约、启动对账、工具副作用意图与回执     │
│  core/chat/runtime：Run 准入、聊天 worker、续播、steer、压缩         │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│  智能体执行                                                          │
│  core/agent_runtime：runner.astream_chat_workflow、路由策略、         │
│    记忆 / 本体 / 进化集成                                            │
│  core/llm：factory/build → AgentScope ReActAgent、工具、上下文、引用 │
│  orchestration/：plans 计划模式 · loops 自主循环 · automations 自动化 │
│  personal_agent/：身份、任务队列、调度、渠道、设备连接、工作区        │
└───────┬──────────────────────┬──────────────────────┬──────────────┘
        │ streamable-http      │ 沙箱驱动协议           │ SQL / 向量
┌───────▼───────────┐ ┌────────▼──────────┐ ┌─────────▼─────────────┐
│ MCP 工具池         │ │ 沙箱与作业          │ │ 数据层                 │
│ mcp 工具进程       │ │ OpenSandbox       │ │ PostgreSQL / SQLite   │
│ 端口见 _ports.py   │ │ script-runner     │ │ Redis · Milvus · Neo4j│
│ 插件工具           │ │ core/jobs 后台作业 │ │ S3 兼容 / 本地存储     │
└───────────────────┘ └───────────────────┘ └───────────────────────┘
```

依赖方向自上而下单向：`api → features / orchestration / personal_agent → core`；`core` 不依赖上层，`integrations/` 承载钉钉、飞书、邮箱等外部系统接入。MCP 服务器是独立进程，与后端只通过 HTTP 通信，崩溃互不影响。

## 领域本体驱动的企业可信控制平面

HugAgentOS 将领域本体作为可执行的控制平面，而不只是一个知识库，
从而把智能体运行时扩展为面向企业的可信 AgentOS。版本化的领域包
（Domain Pack）可从受控概念和关系，逐步延伸到不变式、Action 契约、
工作流、角色、权限和证据要求，让技能、编排和记忆三个引擎使用同一套
业务语义。

下图是在当前 Harness 基础上渐进集成的目标企业架构。现有的确认门、
结构化审查结论、审计记录与版本管理构成底座，本体资产和执行约束则逐步
接入；这一机制用于增强合规与证据驱动审查，不宣称能消除所有自由文本幻觉。

```mermaid
flowchart TB
    U[企业用户与多端渠道] --> API[Web / 桌面端 / API]
    API --> RUN[ChatRun 与流式工作流]

    ONTO[领域本体控制平面<br/>概念 · 关系 · 不变式<br/>Action 契约 · 角色 · 权限]
    ONTO --> BUILD[构建时校验<br/>技能 · 工具 · 子智能体]
    ONTO --> BOOT[领域启动注入<br/>语义对齐 · 相关规则]

    subgraph HARNESS[三引擎 Agent Harness]
        direction LR
        SKILL[技能引擎] <--> ORCH[编排引擎]
        ORCH <--> MEMORY[记忆引擎]
    end

    RUN --> ORCH
    BUILD --> SKILL
    BOOT --> ORCH
    BOOT --> MEMORY
    ORCH --> PLAN[候选计划或 Action]

    PLAN --> RULE[确定性本体规则门]
    ONTO --> RULE
    RULE -->|低风险且合规| EXEC[受控执行]
    RULE -->|检查点或高风险| REVIEW[证据驱动复核]
    REVIEW -->|通过| EXEC
    RULE -->|违规| REVISE[拒绝并返回证据与修复指引]
    REVIEW -->|修订或升级| REVISE
    REVISE --> ORCH

    EXEC --> CAP[MCP · Skills · 沙箱 · RAG]
    EXEC --> AUDIT[可追溯审计与回放]
    REVISE --> AUDIT
    AUDIT --> EVOLVE[受治理的本体演进提案]
    EVOLVE -. 人工审批 · 版本化 · 可回滚 .-> ONTO
```

可信控制平面遵循三项原则，避免把每一步都变成高成本的委员会审查：

1. **确定性优先。** 先执行可机械判定的不变式、权限和 Action 契约。
2. **按风险复核。** 仅检查点、高风险动作或证据不确定时进入深度复核；
   合规的低风险动作可直接通过执行门。
3. **有证据再演进。** 违规、审批和执行结果进入审计链；本体变更提案必须经过
   人工批准、生成新版本并保留回滚路径。

本体与 RAG 相互补充：本体规定智能体可以做什么、需要满足哪些约束，
RAG 则提供支撑决策所需的文档与证据。

## 一次对话请求的完整生命周期

入口为 `POST /v1/agents/responses`（`src/backend/api/routes/v1/chats/__init__.py`），返回 `text/event-stream`。

### 1. 接入与校验

1. Nginx 把 `/api/v1/agents/responses` 转发到 backend。
2. `api/deps.py::get_current_user` 解析 session cookie 或个人 API-Key，二者皆无返回 401。
3. 三个只读查询（能力位解析、用户设置、会话历史）通过 `asyncio.to_thread` 并行执行，各自持独立 DB 会话。
4. 校验子智能体归属、项目挂载权限，落库用户消息，组装 workflow context（`core/chat/context.py`）。

### 2. 启动 Run（与 HTTP 连接解耦）

`core/chat/runtime/admission.py::start_run` 把本轮注册为后台 Run（落 `chat_runs` 表），由持有数据库租约的聊天 worker 执行；HTTP 响应只是「跟随」这个 Run：

- 浏览器断线、刷新页面后可调 `GET /v1/chats/stream/{run_id}?from=N` 从指定偏移续播；
- 运行中可通过 `POST /v1/chat-runs/{run_id}/steer` 追加指令，在下一个安全 ReAct 边界生效；
- 后端启动时 `core/chat/runtime/recovery.py::recover_orphan_runs` 依据最后提交的安全点对账租约过期的 Run：可恢复的继续，不可恢复的明确收尾。

### 3. 编排与智能体执行

`core/agent_runtime/runner.py::astream_chat_workflow` 驱动单轮执行：

1. **记忆检索**——`core/agent_runtime/integrations/memory.py` 读取 L1 画像 / L2 向量记忆（开启时），注入系统消息；
2. **路由决策**——`core/agent_runtime/strategy.py`，`ROUTER_STRATEGY=main_only`（默认）恒路由到主智能体；
3. **构建智能体**——`core/llm/factory/build.py::create_agent_executor`：装配提示词（`prompts/prompt_runtime.py`，DB 版本池优先）、按 catalog 与用户能力位筛选 MCP 服务器、注册自研工具（Read/Write/Edit/Glob/Grep/bash/技能加载等，见 `core/llm/tools/`）、最终一次性构造 AgentScope Toolkit；
4. **流式执行**——`core/llm/runtime/streaming.py::StreamingAgent` 包装 ReActAgent，把推理增量、工具调用、工具结果逐条产出；
5. **引用提取**——`core/llm/citations/` 从工具结果中解析 `[ref:tool_name-N]` 标记生成引用项；
6. **收尾**——落库助手消息与工具调用日志，`core/memory/pipeline.py` 在后台异步抽取记忆（不阻塞 SSE 主链路），独立接口生成追问问题（`core/chat/followups.py`）。

### 4. SSE 事件流

每条事件为 `data: {json}\n\n`，`type` 字段区分种类，流末尾以 `data: [DONE]` 终止：

| 事件 `type` | 含义 | 产出处 |
|---|---|---|
| `content`（`event: ai_message`） | 正文文本增量 | `chats.py::_stream_sse_response` |
| `thinking` | 深度思考增量 | `core/chat/tool_log.py::build_thinking_event` |
| `tool_call_start` | 工具调用开始（稳定 ID + 名称） | `core/chat/tool_log.py` |
| `tool_call_delta` | 合批后的工具参数 JSON 增量 | `core/chat/tool_log.py` |
| `tool_call` | 工具参数完整、即将执行 | `core/chat/tool_log.py` |
| `tool_result` | 工具执行结果（含引用、产物卡片载荷） | `core/chat/tool_log.py` + `core/chat/tool_payloads.py` |
| `tool_progress` | 长工具的进度上报 | `chats.py` |
| `meta` | 末尾元信息：路由、引用源、产物列表等 | `core/agent_runtime/runner.py` |
| `error` | 错误事件（随后立即 `[DONE]`） | `chats.py` |

前端 `src/frontend/src/hooks/useStreaming.ts` 按 `type` 分发，并按 `tool_id` 把 start、delta、最终调用与结果原位合并为一张工具卡片。

## 容器拓扑

`docker-compose.yml` 定义全部服务，按 profile 分组：

| 服务 | 容器名 | 端口 | 说明 |
|---|---|---|---|
| `frontend` | hugagent-frontend | `3002:80` | Nginx：静态资源 + `/api` 反代 |
| `backend` | hugagent-backend | `3001` | FastAPI 主服务 |
| `mcp` | hugagent-mcp | 内部独立端口 | 内置 MCP 服务器常驻进程（streamable-http；数仓查询与行业工具为 EE） |
| `postgres` | hugagent-postgres | `5432` | 主数据库 |
| `redis` | hugagent-redis | `6379` | 会话 / 缓存 |
| `script-runner`（profile `script_runner`） | hugagent-script-runner | 内部 | 可选的轻量脚本执行 sidecar（无 DB/密钥访问权） |
| `opensandbox` + `opensandbox-config-init`（profile `opensandbox`） | hugagent-opensandbox | 内部 | 默认沙箱：持久会话、快照恢复（与 script-runner 互斥） |
| `etcd` / `minio` / `milvus`（profile `mem0`） | hugagent-etcd 等 | 内部 | L2 向量记忆基础设施 |
| `neo4j`（profile `mem0`） | hugagent-neo4j | `7474/7687` | L3 图谱记忆（可选） |
| `personal-agent-opensandbox`（profile `personal-agent`，`docker/personal-agent/compose.personal-agent.yml`） | — | 内部 | Personal Agent 永久云电脑的专用控制器 |

所有服务挂在同一 `hugagent-network` 网络；`postgres_data` 等具名卷跨重建持久化。部署细节见 [Docker Compose 部署](../deployment/docker-compose.md)。

## 关键设计决策

### Catalog 单一真源

`src/backend/core/config/catalog.json` 是能力开关的唯一权威，四类条目：`skills` / `agents` / `mcp` / `kb`。`core/capabilities/catalog/` 暴露 `is_enabled(kind, id)` / `get_enabled_ids(kind)`，从 MCP 工具装载、技能注册到路由决策全部经此门控；管理台的启停操作写 `catalog_overrides` 表叠加在文件之上（`core/capabilities/catalog/loader.py`）。新能力只要进 catalog 即可被前端能力中心与智能体工厂同时感知，无需改编排代码。详见 [能力目录](../modules/catalog.md)。

### 统一响应信封

全部 v1 接口返回 `{ code, message, data, trace_id, timestamp }`，由 `core/infra/responses.py` 的 `success_response` / `error_response` / `paginated_response` 统一生产；SSE 端点统一经 `sse_response` 包装（携带禁用代理缓冲的标准头）。前端 `api.ts::unwrapData` 对应解包，错误码体系见 [错误码](../api/error-codes.md)。

### 提示词 DB 化

系统提示词不再硬编码：版本池存于 `content_blocks` 表（`id=prompt_versions`），由 `core/llm/prompt_versions.py` 管理 CRUD、激活与首启 seed；`prompts/prompt_runtime.py` 装配时按「DB 激活版本 → `prompts/prompt_text/default/system/` 文件兜底 → 最小硬编码兜底」三级降级，保证永不为空。运营可在管理台灰度切换提示词版本而无需发版。详见 [提示词系统](../modules/prompts.md)。

### MCP 工具独立进程

每个工具是 `src/backend/extensions/mcp_servers/<name>/` 下的独立 MCP 服务器，统一运行在专用 `mcp` 容器内，以 streamable-http 常驻监听（端口表 `extensions/mcp_servers/_ports.py` 为单一真源），后端经 `core/llm/mcp/manager.py` 的 HTTP 客户端连接池复用连接。好处：工具崩溃不影响主进程、依赖隔离（文档编辑与导出已迁入沙箱技能）、可独立扩缩。`query_database` 与 `ai_chain_information_mcp` 两个行业工具依赖内网数据源，归商业版 EE。详见 [MCP 工具](../modules/mcp-tools.md)。

### Run 与连接、进程解耦

流式对话不绑定 HTTP 连接，也不绑定某个进程：Run 状态与按步上下文落库，worker 通过数据库租约领取执行，过期执行者不能再提交结果或调用工具；进程中断后由其他 worker 接管。工具调用先记录意图、有回执才算完成（`core/runs/tool_effects/`），恢复时只重放能证明尚未生效的调用，结果未知的外部操作交给用户确认。自动化、计划模式、自主循环与 Personal Agent 复用同一套 Run 机制。

### CE/EE 同源双形态

商业版主仓即全量代码；社区版由 `scripts/build_ce.py` 按 `ce/manifest.yaml` 派生（排除 `edition_ee` 实现 + 文本变换 + CE overlay）。运行时边界包括路由注册表 `api/routes/v1/__init__.py`、建表边界 `edition_ee/db/edition_tables.py`（CE 不注册 20 张 EE 专属表）、EE 的 `edition_ee/licensing/` 与 CE 固定版本中间件，以及站点可见性等版本策略缝隙。详见 [版本与授权](../editions/overview.md)。

## 相关源码

| 主题 | 路径 |
|---|---|
| FastAPI 应用与启动钩子 | `src/backend/api/app.py` |
| 流式对话入口 | `src/backend/api/routes/v1/chats/__init__.py` |
| Run 执行器 | `src/backend/core/chat/runtime/` |
| 流式编排 | `src/backend/core/agent_runtime/runner.py` |
| 智能体工厂 | `src/backend/core/llm/factory/build.py` |
| MCP 配置与端口 | `src/backend/core/config/mcp_config.py`、`src/backend/extensions/mcp_servers/_ports.py` |
| 能力目录 | `src/backend/core/config/catalog.json`、`src/backend/core/capabilities/catalog/` |
| Run、租约与恢复 | `src/backend/core/runs/`、`src/backend/core/chat/runtime/recovery.py` |
| 响应信封 | `src/backend/core/infra/responses.py` |
| 提示词装配 | `src/backend/prompts/prompt_runtime.py` |
| CE/EE 接缝 | `src/backend/api/routes/v1/__init__.py`、`src/backend/edition_ee/db/edition_tables.py`、`src/backend/api/middleware/edition.py`、`src/backend/edition_ee/licensing/` |
| 容器编排 | `docker-compose.yml` |
