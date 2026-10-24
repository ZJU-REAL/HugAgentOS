# 后端架构详解

> 最后更新：2026-07-22

后端位于 `src/backend/`，是一个分层清晰的 FastAPI 单体：API 层只做协议与鉴权，编排层负责把一次对话变成可断线续播的流式 Run，`core/` 承载全部领域逻辑，MCP 工具与脚本执行 sidecar 则以独立进程运行。本文自顶向下逐层拆解。

## 顶层布局

```
src/backend/
├── api/             # FastAPI 应用、中间件与 CE 路由
├── edition_ee/      # 商业版路由、License、Team/RBAC、ORM、Dify 与服务实现
├── orchestration/   # 对话编排：Run 执行器、工作流、策略、引用、调度器
├── core/            # 领域核心：17 个子模块（auth/llm/db/ontology/services/...）
├── prompts/         # 提示词装配运行时 + 文件兜底文本
├── extensions/       # 随包扩展能力
│   ├── skills/       # default / marketplace
│   ├── agents/       # default / marketplace
│   ├── plugins/      # default / marketplace
│   └── mcp_servers/  # MCP process implementations
├── scripts/         # 运维脚本：内容导入导出、入口脚本、初始化 SQL
├── alembic/         # 数据库迁移链（EE 主链；CE 用独立基线，见数据模型篇）
└── tests/           # pytest 测试套件
```

依赖方向单向：`api → orchestration → core`；`extensions/mcp_servers` 与 `core/sandbox/runner` 提供独立进程入口。runner 只共享进程管理基础模块，不加载后端配置或数据库；后端通过 HTTP 调用它。

## core/ 子模块逐个说明

### core/auth — 认证与权限

| 文件 | 职责 |
|---|---|
| `backend.py` | 认证后端抽象：`AUTH_MODE=mock/remote` 分流 |
| `password.py` | 本地账号密码哈希（Argon2id） |
| `roles.py` | 团队角色常量单一真源 |
| `permissions_iface.py` | 权限接口层——CE/EE 拆分接缝 C3，CE 树用 overlay 替换为单租户实现 |
| `session.py`（商业版 EE） | Redis 会话管理 |
| `sso.py`（商业版 EE） | 企业 SSO ticket 交换客户端 |
| `invite.py`（商业版 EE） | 注册码生成与原子消费 |
| `team_permissions.py` / `project_permissions.py` / `chat_share_permissions.py`（商业版 EE） | 团队文件夹 / 项目 / 团队内会话共享的权限解析 |
| `mock_ticket_store.py` | 开发态 mock-SSO ticket 存储 |

### core/llm — 智能体与模型

| 文件 | 职责 |
|---|---|
| `factory/build.py` | 核心工厂 `create_agent_executor`：装配提示词、筛选 MCP、注册工具与技能，产出 AgentScope 2.0 Agent |
| `chat_models.py` | 模型工厂（按 DB 模型配置构造 AgentScope ChatModel） |
| `mcp_manager.py` / `mcp_pool.py` | MCP 客户端池：stable 连接复用、transient 连接随请求关闭 |
| `tool_collector.py` | 把增量 `register_*` 风格适配到 2.0 一次性 Toolkit |
| `middlewares.py` | AgentScope 2.0 中间件（取代 1.x hooks）：pre_reply / post_acting / post_reasoning |
| `hooks.py` | 纯逻辑 helper：文件上下文构建、per-turn 状态、模型解析 |
| `tools/` | 自研工具注册：`read/write/edit/glob/grep_tool.py`（通用文件读写检索工具）、`sandbox_tool.py`（bash + 产物搬运）、`skill_tool.py`（技能加载）、`pin_tool.py`、`read_artifact_tool.py`、`_tool_confirmation/`（共享工具审批与交互） |
| `context_manager.py` / `history_summarizer.py` / `summarizer.py` | 上下文窗口预算、历史结构化摘要、会话摘要 |
| `offloader.py` | 超长工具结果落盘沙箱 `/workspace/.offload` 回读 |
| `subagent_tool.py` | `call_subagent` 工具：主智能体向子智能体派发任务 |
| `classifier.py` / `finish_guard.py` / `system_reminder.py` / `message_compat.py` / `workspace.py` | 会话分类、哑退出补救、带外系统提醒、消息格式互转、pin 工作区状态 |
| `skill_distiller.py`（商业版 EE） | 把对话轨迹蒸馏为技能草稿 |

### core/db — 数据访问

| 文件/包 | 职责 |
|---|---|
| `engine.py` | 引擎、SessionLocal、`init_db` 启动兜底建表 |
| `models/` | ORM 模型包，按领域拆 14 个文件（见 [数据模型](./data-model.md)） |
| `repository/` | 仓储层：`agent/artifact/audit/catalog/chat/kb/team/user.py` |
| `model_repository.py` | 模型供应商 / 角色指派的仓储 |
| `edition_tables.py` | `EE_ONLY_TABLES` + `ce_create_all()`——CE/EE 建表边界单一真源 |

### core/config — 配置与能力目录

| 文件 | 职责 |
|---|---|
| `settings.py` | 集中式应用配置（env 驱动，含 `JX_EDITION` 版本位） |
| `catalog.json` + `catalog.py` | 能力目录单一真源 + `is_enabled` / `get_enabled_ids` 门控 API |
| `catalog_loader.py` / `catalog_migration.py` / `catalog_common.py` | 目录加载、缓存、DB override 合并、形状迁移 |
| `catalog_resolver.py` | 统一能力解析：请求 → 生效的 skill/mcp/kb id 集合 |
| `mcp_config.py` | MCP 服务器连接定义（streamable-http URL 由 `_ports.py` 推导） |
| `display_names.py` / `user_intros.py` | 工具与 MCP 的展示名 / 用户引导文案 |
| `runtime_env.py` | mcp 容器内服务的 DB 化 env 查询 |
| `distillation.py`（商业版 EE） | 技能蒸馏阈值 / 关键词 / cron 默认值 |

### core/services — 业务服务层（59 个服务）

会话域：`chat_service`（会话与消息）、`plan_service`（计划模式）、`automation_service`（定时任务）、`user_agent_service`（自定义子智能体）。

内容域：`artifact_service`（产物 / 我的空间资源）、`kb_service`（知识库）、`catalog_service`（目录 override）、`prompt_version_service`（提示词版本池）、`marketplace_service`（技能市场）、`skill_icon_service`、`skill_deps_aggregator`（技能依赖聚合为沙箱构建清单）。

用户域：`user_service`、`local_user_service`（注册 / 登录 / 改密）、`api_key_service`（个人 API-Key）、`user_folder_service`、`project_service` + `project_file_service` + `project_scope`（项目工作空间）。

配置域：`model_config`（DB 化模型配置，带缓存）、`system_config`（服务配置）、`mcp_service`（MCP 服务器配置）、`log_service`（可观测性日志异步落库）。

本体域：`ontology_service`（用户开关、版本与运行时裁剪）、`ontology_evolution_service`（证据预筛、脱敏、人审草案与未激活版本物化）；`core/ontology/` 收口四层 Schema、构建校验、确定性门禁、工具过滤和提示词渲染。

商业版 EE：`team_service` / `team_folder_service` / `sso_sync`（团队与 SSO 同步）、`distillation_service`（技能蒸馏）、`sandbox_rebuild_service` + `cube_template_builder`（持久沙箱模板重建）、`security_service`（安全管理台只读聚合）。

### core/memory — 三层记忆系统

| 文件 | 职责 |
|---|---|
| `profile.py` | L1 画像记忆：有界 markdown 档案，会话启动冻结注入 |
| `service.py` / `graph.py` | L2：mem0 + Milvus 做法向量；L3：本地 Neo4j 图谱关系（配置组装 + 异步包装） |
| `pipeline.py` | 写入后置流水线——全部记忆写操作剥离出 SSE 主链路 |
| `extractors/` | 4 个 LLM 抽取器（identity/preference/fact/task）+ `router.py` 分类调度 + `writers.py` 落盘分发 |
| `sanitizer.py` | 敏感数据脱敏闸门（规则存 `memory_sanitizer_rules` 表） |
| `context.py` | `MemoryContext` 统一上下文载体 |
| `audit.py`（商业版 EE） | 记忆操作审计留痕（CE 为 overlay stub，不落表） |

### core/sandbox — 沙箱驱动

根目录仅保留协议、异常、工厂和包初始化；按驱动类型与共享职责组织实现。

| 文件/包 | 职责 |
|---|---|
| `protocol.py` / `errors.py` / `factory.py` | 统一接口、数据契约、异常与 `SANDBOX_PROVIDER` 驱动选择 |
| `providers/script_runner/` | script-runner 的 HTTP 客户端与认证 |
| `providers/opensandbox/` | OpenSandbox 驱动、会话创建与生命周期、快照、卷、传输、预热池及恢复（CE/EE 共用） |
| `providers/cube/`（商业版 EE） | 腾讯 CubeSandbox（E2B 兼容 MicroVM）驱动 |
| `providers/evaluation/` | 评测沙箱绑定与进程限制 |
| `providers/edition.py` / `providers/account_env.py` | 版本适配入口与账号环境装配 |
| `runner/` | 独立执行服务；Docker 与桌面统一入口 `core.sandbox.runner.server:app` |
| `processes/` | 前后台进程、输出、截止时间、云端适配及线程/事件循环调度 |
| `sessions/` | 会话注册、路由、绑定、保活与元数据 |
| `workspace/` | 本地/容器/桌面路径、挂载、归档、流式写入与临时凭据清理 |
| `permissions/` | 本地路径和命令权限评估，以及 OS 沙箱准入 |
| `oslayer/` | OS 隔离策略模型、路径规范、策略构建及平台执行实现 |

`runner` 仅依赖不加载后端配置或数据库的进程和路径基础模块。OpenSandbox 专属组件留在自己的驱动目录；Cube 与 OpenSandbox 共用的进程、路径和账号环境能力由共享包提供。

### core/capabilities/skills — 技能引擎

| 文件 | 职责 |
|---|---|
| `loader.py` + `registry.py` | 多源技能加载与注册（skill-creator 对齐的 SKILL.md 规范） |
| `backends/` | 存储后端抽象：`filesystem`（extensions/skills）、`database`（admin_skills 表）、`composite` 合并 |
| `selector.py` | 按用户意图动态挑选技能 |
| `skill_archive.py` | 技能目录 tar.gz 构建缓存，加速沙箱投递 |
| `deps_detector.py` | 从技能附带文件探测 pip/npm/apt 运行时依赖 |
| `binary_files.py` / `cache_refresh.py` / `config.py` | 二进制附件支持、管理操作后缓存失效、多源配置 |

### 其余 core 子模块

| 子模块 | 职责与关键文件 |
|---|---|
| `core/chat` | workflow 上下文组装（`context.py`）、SSE 工具日志事件构造（`tool_log.py`） |
| `core/files/content` | 附件解析（`file_parser.py`）、KB 文档分块/关键词/向量化（`kb_processing.py`）、上传校验（`file_validation.py`）、产物读取与摘要（`artifact_reader/refs/summary.py`）、内容块导入导出（`content_blocks.py`）、`svg_fit.py` |
| `features/knowledge` / `edition_ee/kb` | 自建知识库解析与 Milvus 向量库保留在 `features/knowledge`；Dify 客户端与外部检索适配器只存在于 `edition_ee/kb`（商业版 EE） |
| `core/files/artifacts` | 产物存储 `store.py`：本地 / OSS 双模式 |
| `core/infra` | 统一响应（`responses.py`）、异常（`exceptions.py`）、结构化日志（`logging.py`）、Redis 单例（`redis.py`）、指标（`metrics.py`）、后台任务注册表（`runtime_state.py`）、脱敏（`data_masking.py`）、蒸馏预算闸门（`distillation_budget.py`，商业版 EE） |
| `edition_ee/licensing` | 能力枚举、验签、时钟回拨防护、席位策略、中间件和 manager 全部只存在于商业版 EE；CE 树物理不含 License 包，由 `api/middleware/edition.py` 的 CE overlay 提供固定版本探针 |
| `core/files/storage` | 存储协议 `protocol.py` + 工厂 `factory.py`；`local.py`（CE）、`s3.py` / `oss.py`（商业版 EE） |

## orchestration/ — 编排层

| 文件 | 职责 |
|---|---|
| `chat_run_executor.py` | 把 AI 工作流从 HTTP 连接解耦为后台 Run：启动、SSE 跟随、按偏移续播、崩溃恢复 |
| `workflow.py` | 单轮流式编排主体：记忆注入 → 建 agent → 流式消费 → 引用提取 → meta 收尾 |
| `streaming.py` | AgentScope 2.0 流式包装 `StreamingAgent`，产出标准化事件块 |
| `strategy.py` | 路由策略：`ROUTER_STRATEGY=main_only`（默认）/ `llm_router`（占位，回落 main） |
| `citations.py` | 工具结果 → `[ref:tool_name-N]` 引用项提取 |
| `memory_integration.py` | SSE 主链路外的非阻塞记忆读写整合 |
| `followups.py` | 独立追问问题生成器 |
| `message_parser.py` | 消息内容提取与解析 |
| `registry.py` | Agent 注册表 |
| `tool_payloads.py` | SSE tool_result 载荷构建（产物卡片、技能加载等专用形状） |
| `tool_callbacks.py` | 工具调用软告警（仅观测，不阻断） |
| `subagents/plan_mode.py` | 计划模式子智能体：生成并执行结构化计划 |
| `schedulers/automation_scheduler.py` | 自动化调度器：轮询 DB 到期任务并触发 |
| `schedulers/distillation_cron_scheduler.py`（商业版 EE） | 技能蒸馏每日 cron |

## api/ — API 层

### 应用与中间件

`api/app.py` 创建 FastAPI 应用并以 lifespan 串行执行启动钩子：建表兜底 → Run 恢复 → 过期 Run 收割 → 沙箱池预热 → 页面配置 / 提示词版本 seed → MCP 目录同步 → 预加载 → 自动化与蒸馏调度器 → 记忆预热。中间件在 `api/middleware/`：`cors.py`、`logging.py`（结构化请求日志 + trace_id）、`error_handler.py`（异常 → 统一错误信封）。`api/deps.py` 提供鉴权与用户解析依赖，`api/health.py` 提供健康检查，`core/chat/schemas.py` 收口公共 Pydantic 模型。

### 路由注册表（CE/EE 接缝 C1）

全量仓库由 `api/routes/v1/__init__.py` 的 `CE_ROUTERS` 与 `edition_ee/routes/registry.py` 的 `EE_ROUTERS` 组合注册（当前各 36 项）。EE 表项携带 License 能力位，由 `edition_ee/licensing/deps.py` 做第二道防线（第一道是 CE 派生树物理删除 EE 文件）；CE overlay 把 `EE_ROUTERS` 固定为空。`config_verify` / `config_license` / `auth` 三项显式豁免，保证 License 失效时仍能登录和换证。

### 路由文件分组

CE 路由位于 `api/routes/v1/`；已物理拆分的商业路由位于 `edition_ee/routes/`，其余历史管理路由由 EE 注册表按能力位挂载。

| 分组 | 文件 |
|---|---|
| 会话与流式 | `chats.py`（流式 SSE 主入口）、`chat_runs.py`、`chat_shares.py`、`summary.py`、`classify.py`、`memories.py` |
| 内容与文件 | `content.py`、`file_upload.py`、`file_parse.py`、`artifacts.py`、`myspace_folders.py`、`kb.py`（+ `kb_models.py` 纯 Pydantic 模型）、`projects.py` |
| 能力与配置 | `catalog.py`、`models.py`、`config.py`、`marketplace.py`、`me_capabilities.py`（能力中心自助）、`ontologies.py`（用户开关、版本治理与证据闭环）、`agents.py`、`plans.py`、`automations.py`、`meta.py`（版本 / 能力位探针，无鉴权） |
| 用户与认证 | `users.py`、`me.py`、`api_keys.py`、`mock_sso.py`（开发态） |
| 内容管理台（商业版 EE，`/admin` 后端） | `admin_skills.py`、`admin_prompts.py`、`admin_kb.py`、`admin_agents.py`、`admin_mcp_servers.py`、`admin_marketplace.py`、`admin_skill_drafts.py`、`admin_sandbox.py`、`admin_logs.py`、`admin_usage_logs.py`、`admin_billing.py`、`admin_chat_history.py` |
| 系统管理台（商业版 EE，`/config` 后端） | `config_users.py`、`config_teams.py`、`config_invites.py`、`config_security.py`、`config_verify.py`、`config_license.py`、`service_configs.py` |
| 其他商业版 EE | `auth.py`（SSO ticket 交换）、`audit.py`、`team_files.py`、`data_sources.py`、`db_metadata.py`、`gateway_admin.py`、`gateway_anthropic.py` |

## extensions/mcp_servers/ 与 sidecar

`extensions/mcp_servers/` 下的服务器：`internet_search_mcp`、`web_fetch_mcp`、`automation_task_mcp`、`skill_manager_mcp`、`retrieve_dataset_content_mcp`，以及依赖内网数据源的 `query_database_mcp`、`ai_chain_information_mcp`（商业版 EE）。公共设施：`_launcher.py`（按注册表的安装位置和端口启动服务进程）、`_serve.py`、`_common.py`、`_retrieve_cleaning.py`。Word / Excel 等文档导出统一由沙箱中的 Agent 技能负责。

`core/sandbox/runner/server.py` 是技能脚本执行 sidecar：独立容器、受限子进程（resource 限额）、无数据库 / Redis / API-Key 访问权。

## prompts/ — 提示词装配

`prompt_runtime.py` 是装配入口：DB 激活版本（`content_blocks` 的 `prompt_versions`）→ `prompt_text/default/system/*.md` 文件兜底 → 最小硬编码兜底。`prompt_config.py` / `provider.py` 提供可插拔配置与加载；`project_section.py`、`kb_lite_section.py` 渲染项目模式段与轻量 KB 目录段；`prompt_text/` 下另有 `code_exec` / `distillation` / `plan_mode` 场景提示词。

## 分层原则小结

1. **api 只做协议**：参数校验、鉴权、信封包装，不写业务逻辑；
2. **orchestration 只做编排**：把领域服务串成流式工作流，不直接操作 ORM；
3. **core/services 是唯一业务入口**：路由不得绕过服务层直查 `core/db/models`（少量只读快路径除外）；
4. **进程边界即故障边界**：MCP、script-runner、沙箱均独立进程 / 容器，与后端只过协议层；
5. **CE/EE 接缝集中**：路由注册表、`edition_tables`、`permissions_iface` 与版本中间件集中收口，商业实现进入 `edition_ee`，业务代码不散落 `if edition` 判断。

## 相关源码

| 主题 | 路径 |
|---|---|
| 应用入口与启动钩子 | `src/backend/api/app.py` |
| 路由注册表 | `src/backend/api/routes/v1/__init__.py` |
| 智能体工厂 | `src/backend/core/llm/factory/build.py` |
| Run 执行器 / 工作流 | `src/backend/core/chat/runtime/`、`workflow.py` |
| 能力目录 | `src/backend/core/capabilities/catalog/` |
| 沙箱协议 | `src/backend/core/sandbox/protocol.py` |
| 记忆流水线 | `src/backend/core/memory/pipeline.py` |
| EE License 实现 / CE 版本中间件 | `src/backend/edition_ee/licensing/`、`src/backend/api/middleware/edition.py` |
| 技能引擎 | `src/backend/core/capabilities/skills/loader.py` |
| MCP 注册表与端口 | `src/backend/extensions/mcp_servers/registry.py`、`_ports.py` |

空间 MCP 接口位于 `extensions/mcp_servers/space_files_mcp/`。注册表分别管理安装位置和暴露方式；混合模式通过桌面能力网关访问空间，修改操作接入统一审批。详见[空间文件挂载](../deployment/space-files.md)。
