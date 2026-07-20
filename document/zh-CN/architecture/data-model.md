# 数据模型概览

> 最后更新：2026-07-19

数据访问层位于 `src/backend/core/db/`：ORM 模型按领域拆为 `models/` 包（14 个领域文件，全部经 `models/__init__.py` 原样 re-export，旧的 `from core.db.models import X` 写法不变），仓储层在 `repository/` 包，引擎与会话在 `engine.py`。开发环境用 SQLite、生产用 PostgreSQL——`models/__init__.py` 定义 `JSONType`（PostgreSQL 自动升级为 JSONB）与 `INETType`（PostgreSQL 用 INET）两个方言感知类型，全部模型共用。

```
core/db/
├── engine.py            # 引擎、SessionLocal、init_db 启动兜底建表
├── models/              # ORM 模型包（按领域 14 个文件）
│   ├── identity.py      # 用户、团队、文件夹、API-Key
│   ├── chat.py          # 会话、消息、Run、反馈、沙箱快照
│   ├── project.py       # 项目工作空间
│   ├── knowledge.py     # 知识库、目录 override
│   ├── artifact.py      # 产物、内容块
│   ├── config.py        # 模型供应商、系统配置、定价
│   ├── admin.py         # 管理台资产：技能、提示词、MCP、市场
│   ├── agent.py         # 子智能体、计划模式
│   ├── automation.py    # 定时任务、蒸馏、批量计划
│   ├── logs.py          # 工具/子智能体/技能调用日志、审计
│   ├── memory.py        # 画像记忆、记忆审计、脱敏规则
│   ├── datasource.py    # 数据源、元数据治理与黄金 SQL
│   ├── site.py          # 站点、连接器与渠道配置
│   └── ontology.py      # Domain Pack 版本、门禁、委员会与演进草案
├── repository/          # 仓储层：agent/artifact/audit/catalog/chat/kb/team/user
├── model_repository.py  # 模型供应商/角色指派仓储
└── edition_tables.py    # CE/EE 建表边界单一真源
```

## 表分组一览

标注「（商业版 EE）」的表在 `EE_ONLY_TABLES` 集合内，社区版不建（见下文建表边界）。

### 用户与团队（models/identity.py）

| 表 | 用途 |
|---|---|
| `users_shadow` | 用户影子表：本地注册或用户中心同步的用户主档，`metadata` JSONB 存用户级开关 |
| `local_users` | 本地账号敏感信息（Argon2id 密码哈希、状态、联系方式），与 users_shadow 1:1 |
| `user_folders` | 我的空间个人文件夹树（NULL parent 即根） |
| `user_api_keys` | 个人 API-Key——以用户身份经 HTTP 调用智能体 |
| `invite_codes`（商业版 EE） | 注册码：一次性使用，可预绑团队与角色 |
| `teams`（商业版 EE） | 团队，可由外部 SSO 部门自动建立（source=sso_auto） |
| `team_members`（商业版 EE） | 团队成员（用户 N:M 团队，带角色） |
| `team_folders`（商业版 EE） | 团队文件夹树 |

### 会话与消息（models/chat.py）

| 表 | 用途 |
|---|---|
| `chat_sessions` | 会话主表（标题、模式标记、项目挂载、共享范围等） |
| `chat_session_user_states` | 会话 × 用户的 per-user 状态（pin / favorite） |
| `chat_messages` | 消息表：角色、内容、工具调用 JSON、附加数据 |
| `chat_runs` | 流式 Run：把 AI 任务从 HTTP 连接解耦，支持断线续播与崩溃恢复 |
| `message_feedback` | 消息点赞 / 点踩与评语 |
| `chat_sandbox_snapshots` | 会话级持久沙箱快照指针（配合 OpenSandbox 恢复环境） |

### 项目与产物、内容块（models/project.py、models/artifact.py）

| 表 | 用途 |
|---|---|
| `projects` | 项目（工作空间，personal / team 两种 kind；team 字段 CE 恒 NULL） |
| `project_favorites` | 项目 star（每人独立，不影响他人视图） |
| `artifacts` | 产物表：用户上传文件与 AI 生成文件（报告、图表等）的统一登记 |
| `content_blocks` | 可编辑内容块 KV：版本说明、能力中心文案、**提示词版本池**（`id=prompt_versions`）、提示词广场（`id=prompt_hub`）等 |

### 知识库（models/knowledge.py）

| 表 | 用途 |
|---|---|
| `kb_spaces` | 知识库空间 |
| `kb_documents` | 知识库文档（上传、解析、索引状态） |
| `kb_chunks` | 父分块存储（父子分块检索的上下文层；子块向量在 Milvus） |
| `catalog_overrides` | 能力目录的运行时启停 override（叠加在 catalog.json 之上） |

### 模型与系统配置（models/config.py）

| 表 | 用途 |
|---|---|
| `model_providers` | OpenAI 兼容模型端点（DB 化模型配置） |
| `model_role_assignments` | 角色 → 模型映射（main / summarizer / router 等，每角色至多一个） |
| `system_configs` | 外部服务配置 KV（数仓、KB、行业接口、文件解析器） |
| `model_pricing`（商业版 EE） | Token 计费的模型定价 |

### 技能与 MCP（models/admin.py）

| 表 | 用途 |
|---|---|
| `admin_skills` | DB 化技能（管理员全局技能 + 用户私有技能，owner 隔离） |
| `admin_prompt_parts` | DB 化提示词分段（覆盖文件系统提示词；CE 运行时也读，不在 EE 集合） |
| `admin_mcp_servers` | DB 化 MCP 服务器配置（含用户自助添加的远程 HTTP/SSE MCP） |
| `marketplace_submissions` | 用户私有技能「申请上架技能市场」记录（CE 保留提交端点） |
| `marketplace_visibility_grants`（商业版 EE） | 市场条目可见范围白名单（技能/插件/子智能体市场通用，按用户/团队/角色授权） |
| `admin_skill_drafts`（商业版 EE） | 自动蒸馏产出的候选技能草稿，待管理员审核 |
| `sandbox_rebuilds`（商业版 EE） | 管理员触发的沙箱镜像重建记录 |

### 智能体与计划（models/agent.py）

| 表 | 用途 |
|---|---|
| `user_agents` | 自定义子智能体（管理员或用户创建，绑定技能 / MCP / KB） |
| `plans` / `plan_steps` | 计划模式的计划与步骤（预期技能 / 智能体、执行状态） |

### 自动化与批量（models/automation.py）

| 表 | 用途 |
|---|---|
| `scheduled_tasks` | 自动化定时任务（cron 表达式、重试策略） |
| `scheduled_task_runs` | 定时任务执行记录 |
| `batch_plans` | 批量执行计划（batch_plan MCP 工具生成，确认后由 BatchOrchestrator 执行） |
| `distillation_runs`（商业版 EE） | 技能蒸馏任务队列 + 审计（每 chat_id 至多一行） |

### 记忆（models/memory.py）

| 表 | 用途 |
|---|---|
| `profile_memory` | L1 画像记忆：bounded markdown 档案，会话启动冻结注入（L2/L3 向量与图谱在 Milvus / Neo4j，不落关系库） |
| `memory_sanitizer_rules` | 运行时追加 / 禁用的敏感词脱敏规则（CE 也读，不在 EE 集合） |
| `memory_audit`（商业版 EE） | 记忆读写 / 删除 / 拒写的全链路审计 |

### 日志与审计（models/logs.py）

| 表 | 用途 |
|---|---|
| `tool_call_logs` | 每次 MCP / 内置工具执行一行 |
| `subagent_call_logs` | 子智能体 / 计划步骤的完整执行记录 |
| `skill_call_logs` | 技能触发记录（view / run_script / auto_load） |
| `audit_logs`（商业版 EE） | 用户态关键操作审计 |

### 领域本体（models/ontology.py）

| 表 | 用途 |
|---|---|
| `ontology_packs` | Domain Pack 身份、启用/默认标记与激活版本指针 |
| `ontology_pack_versions` | 不可变的四层本体版本内容与校验报告 |
| `ontology_enforcement_events` | 构建、工具和输出门禁的追加式证据 |
| `ontology_review_runs` | checkpoint / committee 评审裁决、证据与延迟 |
| `ontology_drafts` | 待人工审查的演进候选及物化版本指针 |

## Alembic 迁移机制

- **商业版主链**：`src/backend/alembic/versions/` 下 53 个迁移，从初始建表一路演进（含 MCP 迁往 streamable-http、办公 MCP 下线改技能等结构性变更）。常用命令：`alembic upgrade head`、`make migrate-new msg="..."`（autogenerate 基于 `core/db/models` 元数据）；
- **启动兜底**：`api/app.py` lifespan 的 `_startup_ensure_tables` 调 `core/db/engine.py::init_db`，对 SQLite 开发库幂等补建缺表；
- **社区版独立链**：CE 派生树整体排除主链迁移，overlay 提供单一基线 `ce/overlay/src/backend/alembic/versions/ce_0001_initial.py`——以 SQLAlchemy 元数据为源、按 `EE_ONLY_TABLES` 过滤后 `create_all`，方言感知（SQLite / PostgreSQL 通吃）；后续 CE schema 演进在该链上追加常规迁移。

## CE/EE 建表边界（core/db/edition_tables.py）

`core.db.models` 包在两版共用（EE 表类定义本身无害），但 CE 不应建出 EE 专属空表。`EE_ONLY_TABLES` 是这一边界的单一真源，共 18 张表：

```
teams · team_members · team_folders · invite_codes        # 多租户 / SSO / 邀请
roles · role_assignments                                  # 组织角色权限体系
kb_grants                                                 # 知识库逐用户 / 团队授权
audit_logs · memory_audit                                 # 审计（CE 的 memory audit 为 stub，不落表）
model_pricing                                             # 计费
data_sources · ds_table_meta · ds_column_meta · ds_golden_sql # 数据源 / 元数据治理
gateway_virtual_keys                                      # 对外模型网关虚拟密钥镜像
sandbox_rebuilds · admin_skill_drafts · distillation_runs # 持久沙箱重建 / 技能蒸馏
```

`ce_create_all(bind)` 在 **克隆的 MetaData** 上建出全部非 EE 表：CE 表里指向 EE 表的跨边界外键（如 `projects/artifacts → teams/team_folders`，方案 D3「列保留、恒 NULL」）若原样下发，PostgreSQL 会因引用表不存在而失败——因此在克隆上摘除这些约束（列保留、原 metadata 不动、ORM 映射不受影响）。两个建表入口同源同滤：`init_db` 的 CE 分支（`JX_EDITION=ce` 时过滤）与 CE 迁移基线 `ce_0001`。维护规则：新增 EE 专属模型必须同步加进 `EE_ONLY_TABLES`，集合名与 metadata 实表名做启动断言，防止改名漏更新悄悄退化为全量建表。

几个「看着像 EE 实际 CE 必需」的表刻意不在集合内：`admin_prompt_parts`（提示词运行时读）、`memory_sanitizer_rules`（脱敏闸门无条件查询）、`admin_skills` / `admin_mcp_servers`（个人自助能力，owner 隔离）、`marketplace_submissions`（CE 保留提交端点）。

## 相关源码

| 主题 | 路径 |
|---|---|
| ORM 模型包 | `src/backend/core/db/models/` |
| 本体仓储 | `src/backend/core/db/repository/ontology.py` |
| 引擎与启动建表 | `src/backend/core/db/engine.py` |
| 仓储层 | `src/backend/core/db/repository/` |
| CE/EE 建表边界 | `src/backend/core/db/edition_tables.py` |
| EE 迁移主链 | `src/backend/alembic/versions/` |
| CE 迁移基线 | `ce/overlay/src/backend/alembic/versions/ce_0001_initial.py` |
| 提示词版本池服务 | `src/backend/core/services/prompt_version_service.py` |
| 版本边界权威方案 | [版本与授权](../editions/overview.md) |
