# 后端架构参考

## 1. 顶层布局与依赖方向

```
src/backend/
├── api/              HTTP 协议层：app.py（组装）、deps.py、middleware/、routes/v1/（CE_ROUTERS）、transports/run_sse.py、startup/
├── core/             内核：领域逻辑与可复用运行时（不导入 features / integrations / personal_agent / orchestration）
├── orchestration/    模式编排：plans/、loops/、automations/、workers/（周期任务与恢复）
├── features/         可安装的产品功能模块（各自 README + extension.py + models.py + api/）
├── integrations/     外部系统集成：channels（IM 机器人框架）、dingtalk、lark、email、yida、api
├── personal_agent/   Personal Agent 模块（同样经 extension.py 接入）
├── edition_ee/       商业版专属：路由注册表、License、Team/RBAC、EE 表、KB provider、EE 服务（CE 树物理删除）
├── extensions/       随包资源：skills/、agents/、plugins/（default + marketplace）、mcp_servers/；根路径唯一来源 paths.py
├── prompts/          提示词装配运行时 + prompt_text/ 文件兜底
├── hugagent_cli/     命令行客户端
├── alembic/          迁移（EE 主链；CE 用独立 ce_0001 基线）
└── tests/            pytest（按领域子目录：api/ chat/ core/ features/<module>/ infra/ llm/ orchestration/ …）
```

```
api ──► orchestration ──► core
 │            │             ▲
 └──► features / integrations / personal_agent ──┘   （通过 core.harness.registry 注册钩子接入）
edition_ee ──► core / features                      （CE 派生树中不存在）
```

- **core 永远不导入功能模块的实现**。功能模块在 `extension.setup()` 里调用 `core.harness.registry.register_*` 把能力挂进来；
  关闭时什么也不注册，core 的行为等同于未安装。
- **不保留转发垫片**：模块搬家时直接改所有调用方的 import，不留旧路径 re-export（ADR 20261021）。
- 单个模块 ≤ 500 行（存量超限文件已登记为例外，不要新增）。

## 2. core/ 子包职责

| 子包 | 职责 |
|---|---|
| `agent_runtime/` | 一次普通智能体回合：`runner.astream_chat_workflow` 选主智能体 / 直连路径；`prepare_*`（建 agent、组装输入）、`stream_*`（模型与工具事件 → 既有事件协议）、`*_state`（单轮有类型状态）、`integrations/`（记忆、压缩、本体证据/修复、演化） |
| `runs/` | 持久化执行所有权：租约 / 心跳、取消、事件日志与续播（`events/`）、看门狗、工具副作用账本（`tool_effects/`）、恢复 |
| `chat/` | 会话与消息读写、模式（modes）、分叉、引用、追问、压缩；`runtime/`（聊天 worker：消费回合事件、在 runs 租约下持久化输出、排队 / 插话 / 暂停 / 恢复）；`operations/`、`projection/` |
| `llm/` | AgentScope 适配与智能体装配：`factory/`（build / assembly / tools / prompts）、`runtime/`（流式循环、钩子适配、工具执行、人机交互）、`citations/`（锚点发号）、`mcp/`、`subagents/`、`tools/`、`tool_permissions/`、提示词版本池 |
| `models/` | 模型供应商与选择：chat_models、providers、failover、reasoning effort、embedding / reranking、vision、用量 |
| `capabilities/` | 能力生命周期：`catalog/`、`skills/`、`plugins/`（含插件资源）、`mcp/`、`agents/`、manifest 协议 |
| `harness/` | 宿主接缝：`registry/`（功能模块注册钩子的唯一入口）、执行清单、观测事件、日志写入、用量 |
| `auth/` | `backend.py`（`get_current_user` / `UserContext`）、`bearer.py`（mock/remote Bearer 异步校验 + 共享缓存）、session、本地账号、API Key、能力位、权限接口 |
| `db/` | `engine.py`（Base / SessionLocal / `get_db` / 线程预算）、`threads.py`（`run_db` / `run_in_session` / `off_loop`）、`models/`（核心 ORM 包）、`repository/`、`utc_datetime.py`、各类升级 / 修复脚本 |
| `config/` | `settings.py`（`settings` 冻结 dataclass：deploy / auth / sso / db / llm / memory / storage / kb / redis / server / sandbox / edition / branding …）、`catalog.json`、`mcp_config.py`、`display_names.py`、`runtime_env.py` |
| `infra/` | `responses.py`、`exceptions.py`、`logging.py`、`time.py`（`utc_now`）、`background.py`（`spawn`）、redis、metrics、限流 / 熔断、leader 选举、加密、脱敏 |
| `files/` | 产物（artifacts）、内容解析（content）、空间文件（spaces）、存储协议（storage：local / s3 / oss） |
| `projects/` | 项目、项目环境、指令、本地工作区、个人项目作用域 |
| `memory/` | 分层记忆（L1 画像 / L2 向量 / L3 图谱）、抽取流水线、outbox、脱敏、TTL |
| `ontology/` | 本体治理：schema、校验、运行时闸门、输出评审、修订 |
| `evolution/` | 协同进化共享契约与证据（控制面在 EE） |
| `jobs/` | 后台作业运行时（作业 SDK、监督、通知、文件） |
| `sandbox/` | 沙箱协议 / 工厂 / providers（script_runner、opensandbox、cube[EE]）、runner 进程服务、进程 / 会话 / 工作区 / 权限 / OS 隔离 |

## 3. 功能模块（features / integrations / personal_agent）

`features/installed.py::INSTALLED_FEATURES` 是全量安装清单（CE 由 `ce/overlay/src/backend/features/installed.py` 覆盖）。

| 模块 | 内容 |
|---|---|
| `features/automation` | 定时任务：调度、执行、投递目标、通知、远程副作用 |
| `features/knowledge` | 自建知识库：解析、分块、向量、资产索引、Wiki；外部 provider 接缝 |
| `features/marketplace` | 技能 / 智能体 / MCP 市场：目录、安装、发布、版本、导出 |
| `features/workbench` | 可扩展工作台：应用目录、外部应用 / 嵌入 / 外部认证、插件 UI、资源实例、偏好、诊断、迁移 |
| `features/hosted_apps` | 托管业务应用（SQL / MCP）：部署、数据、发布、资源 |
| `features/sites` | 站点托管与发布（静态服务、访问密码、打包、本机发布） |
| `features/desktop` | 桌面双端：能力桥、网关、本机能力、登录桥接、可观测性 |
| `features/evaluation` | 评测沙箱（严格 OpenSandbox） |
| `features/distillation` · `security_console` · `datasources` · `sandbox_ops` | EE：技能 / 人格蒸馏、安全台、数据源、沙箱运维 |
| `integrations/channels` + `dingtalk` / `lark` / `email` / `yida` | 入站渠道机器人框架与各平台适配 |
| `personal_agent` | Personal Agent：会话、渠道、调度、投递、设备连接、工作区人格 |

模块约定：

```
features/<name>/
├── README.md        负责什么、公开入口、依赖方向、版本差异
├── extension.py     NAME + setup() -> bool；可选 register_tools / register_gateway_contributions /
│                    register_catalog_options / register_retrieval_context
├── models.py        ORM（按安装清单加载，与运行开关无关）
├── edition.py       （可选）CE/EE 差异唯一入口，CE overlay 只覆盖它
├── service.py / repository.py / <领域>.py
└── api/             路由文件（多数进 CE_ROUTERS / EE_ROUTERS，可关闭的由 setup() 内 register_router 挂载）
```

## 4. 启动与路由装配（api/app.py）

1. `orchestration.configuration.register_configuration()`、`features.register_run_capabilities()`、
   `register_model_credentials()`、`integrations.register_credentials()`
2. 挂 health、`api/routes` 文件下载路由
3. `iter_edition_routers(CE_ROUTERS)` 全部 include；`iter_edition_routers(EE_ROUTERS)` include 并附加
   `edition_router_dependencies(policy_key)`（License 能力位，未授权 402）
4. `register_product_tools()` / `register_catalog_options()` / `register_package_sources()` / `register_resource_owners()`
5. `load_extensions(INSTALLED_FEATURES)`：先 `load_models`，再逐个 `<pkg>.extension.setup()`；然后挂
   `core.harness.registry.routers()` 里登记的模块路由
6. `sites_serve_router`（静态站点兜底）→ 登录路由 → mock SSO（按登录模式）→ 本地模式前端静态托管（最后）

注册表规则：

- `CE_ROUTERS`（`api/routes/v1/__init__.py`）：二元组 `(module, attr)`；短名解析为 `api.routes.v1.<module>`，
  含点号的写绝对路径（`"features.sites.api.sites"`）。**顺序即注册顺序**，同前缀族的先后有语义（例：`plugin_ui` 必须在
  `plugins` 之前，否则 `/v1/plugins/{slug}` 会吞掉 `/v1/plugins/ui-contributions`）
- `EE_ROUTERS`（`edition_ee/routes/registry.py`）：三元组 `(module, attr, policy_key)`；`policy_key` 取
  `edition_ee/licensing/features.py::Feature` 的值（`content_admin` / `audit` / `multi_tenancy` / `system_config` /
  `billing` / `model_gateway` / `evolution_control` …）；`None` 仅用于 License 失效时也必须可达的入口（登录、换证）
- `api.edition.EE_ROUTERS` 是懒加载接缝；CE overlay 把它换成空表
- **不要**在 `app.py` 手工 `include_router()`

## 5. 一次聊天回合的流转

```
POST /v1/agents/responses                       api/routes/v1/agent_responses.py（+ chats/ 子路由）
  → 准入与回合登记                               core/chat/runtime/admission.py · core/runs（租约、事件日志）
  → 聊天 worker                                  core/chat/runtime/worker.py（消费事件、按顺序持久化输出）
    → core/agent_runtime/runner.astream_chat_workflow
      → prepare_main / prepare_direct            建 agent、组装输入（模式 core/chat/modes.py、记忆 integrations/memory.py）
        → core/llm/factory/build.py              AgentScope agent：模型选择、工具集、MCP（core/llm/mcp）、技能、提示词
      → stream_main / stream_direct              模型与工具事件 → 事件协议（core/llm/runtime/streaming.py、stream_events.py）
        → core/llm/citations/                    引用锚点发号（e1/e2…）
      → 结束后：本体评审、记忆 / 演化结算、追问（core/chat/runtime/followups.py）
  → SSE 输出                                     api/transports/run_sse.py（跟随事件日志；续播 GET /v1/chats/stream/{run_id}/subscription）
```

平级编排入口（不走普通回合）：

| 入口 | 位置 |
|---|---|
| 计划模式 | `orchestration/plans/`（generate_run / execute_run / mode） |
| 自主循环 | `orchestration/loops/`（driver / planner / reviewer / requirements / recovery） |
| 定时任务派发 | `orchestration/automations/` + `features/automation` |
| 周期任务 / 恢复 | `orchestration/workers/`（`_worker_base.py`、run_recovery、memory_ttl、evolution、蒸馏 cron） |
| 渠道入站 | `integrations/channels/`（归一为入站消息后复用聊天回合） |

## 6. 数据访问与事件循环

- 同步 SQLAlchemy Session 的每次查询都是阻塞 I/O。
- **只用 Session 不 await 的路由写 `def`**（FastAPI 线程池执行）；`def` 路由里没有事件循环，不能 `create_task` / `get_running_loop`。
- **要 await 网络的 async 代码不持有 Session**：每段 DB 工作写成同步函数，用 `await run_in_session(fn, ...)`（新开并关闭会话）
  或 `await run_db(fn, ...)`（已有会话）在线程里跑；需要 async 接口的纯 DB 函数用 `@off_loop` 包装。
- 线程池与连接池按 `core.db.engine.thread_budget()` 同一预算配置（`apply_thread_budget()`）。
- 跨请求存活的工作：落库交给 worker（`orchestration/workers/_worker_base.py`）；事件循环上的临时任务用 `core.infra.background.spawn(coro, name=...)`。
- 守卫测试：`tests/test_no_blocking_async_routes.py`、`tests/test_no_event_loop_in_sync_routes.py`、
  `tests/infra/test_no_session_on_event_loop.py`（全仓零例外：任何 async 函数都不收 `db` / `Session` / `Depends(get_db)`、
  不调 `SessionLocal()`、不 `with … as db`；测试里覆盖 `get_db` 时同时用 `tests/db_support.py` 让 `run_in_session` 指向同一个库）。

## 7. 模块索引（常用入口）

| 主题 | 路径 |
|---|---|
| 应用组装 | `api/app.py`、`api/lifecycle.py`、`api/startup/` |
| 路由注册表 | `api/routes/v1/__init__.py`（CE）、`edition_ee/routes/registry.py`（EE）、`core/harness/registry`（模块路由） |
| 依赖注入 | `api/deps.py`（require_admin / require_config / require_admin_or_config / require_system_settings / require_ontology_governance / require_super_admin / console_actor） |
| 认证 | `core/auth/backend.py`（get_current_user / UserContext）、`core/auth/bearer.py` |
| DB | `core/db/engine.py`（get_db）、`core/db/threads.py`、`core/db/models/`、`core/db/repository/` |
| 信封 / 异常 | `core/infra/responses.py`、`core/infra/exceptions.py`、`api/middleware/error_handler.py` |
| 配置 | `core/config/settings.py`、`core/config/catalog.json` |
| 智能体回合 | `core/agent_runtime/`、`core/llm/factory/`、`core/llm/runtime/` |
| 运行与续播 | `core/runs/`、`core/chat/runtime/`、`api/transports/run_sse.py` |
| MCP 服务 | `extensions/mcp_servers/`（`registry.py`、`_ports.py` 端口稳定契约、`_launcher.py`）、`core/config/mcp_config.py` |
| 技能 / 插件 / 智能体资源 | `extensions/{skills,plugins,agents}/{default,marketplace}`、`core/capabilities/` |
| 提示词 | `prompts/prompt_runtime.py`（DB 版本池优先，`prompt_text/` 兜底）、`core/llm/prompt_*.py` |
| License | `edition_ee/licensing/`（features / deps / gate / manager）、`api/middleware/edition.py` |
| CE 派生 | `ce/manifest.yaml`（排除）、`ce/overlay/`（覆盖接缝文件） |
