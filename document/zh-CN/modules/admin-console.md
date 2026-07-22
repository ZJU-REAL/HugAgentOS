# 管理台

> 最后更新：2026-06-11

HugAgentOS 提供**两个相互独立的管理入口**，分别面向内容运营和系统管理两类角色：

| 入口 | 前端 | 凭证 | 定位 |
|---|---|---|---|
| `/admin` 运营管理台 | `src/frontend/src/AdminApp.tsx` | `ADMIN_TOKEN` | 面向内容运营：功能更新、能力中心、技能、知识库、子智能体等「给用户看什么 / 用什么」 |
| `/config` 系统管理台 | `src/frontend/src/ConfigApp.tsx` | `CONFIG_TOKEN` | 面向系统管理员：模型 / MCP / 提示词配置、用户与权限、监控计费、安全审计、License |

入口分流在 `src/frontend/src/main.tsx`：按 `window.location.pathname` 前缀渲染 `AdminApp`（`/admin`）、`ConfigApp`（`/config`）、`ApiDocApp`（`/api-docs`）或主应用。两台之间有互跳按钮（`/admin` 顶栏「系统配置」→ `/config`；`/config` 顶栏「内容管理」→ `/admin`）。两类令牌的鉴权机制见 [认证与权限](auth.md)。

后端管理路由的 CE/EE 归属以 `src/backend/api/routes/v1/__init__.py` 与 `src/backend/edition_ee/routes/registry.py` 组成的注册表为唯一真源：CE 派生树**物理不包含** EE 路由文件（第一道防线），EE 部署再按 License 能力位（`content_admin` / `billing` / `audit` / `multi_tenancy` / `system_config`）做二次守卫（第二道防线，`edition_ee/licensing/deps.py::requires_feature`）。下文逐组标注。

## /admin 运营管理台

`AdminApp.tsx` 以 Tabs 组织九个面板（组件在 `src/frontend/src/components/admin/`）：

| Tab | 组件 | 后端路由 |
|---|---|---|
| 功能更新 | `UpdatesEditor` | `content.py`（`docs_updates` 内容块） |
| 能力中心 | `CapsEditor` | `content.py`（`docs_capabilities` 内容块） |
| 技能管理 | `SkillsEditor` | `admin_skills.py` |
| 待审草稿 | `SkillDraftsPanel` | `admin_skill_drafts.py` |
| 沙盒依赖 | `SandboxDepsManager` | `admin_sandbox.py` |
| 知识库管理 | `KnowledgeBaseManager` | `admin_kb.py` |
| 提示词中心 | `PromptHubEditor` | `content.py`（`prompt_hub` 内容块） |
| 子智能体 | `AdminAgentManager` | `admin_agents.py` |
| 操作手册 | `ManualEditor` | `content.py`（manual PDF 上传） |

## /config 系统管理台

`ConfigApp.tsx` 以左侧 Menu 组织五组面板（组件在 `src/frontend/src/components/config/` 与复用的 `components/admin/`）：

| 分组 | 面板 | 后端路由 |
|---|---|---|
| 基础配置 | 系统配置 / 页面配置 / 应用配置 / 模型管理 / MCP 工具 / 提示词管理 | `service_configs.py`、`content.py`、`models.py`、`admin_mcp_servers.py`、`admin_prompts.py` |
| 用户与权限 | 用户管理 / 团队管理 / 注册码 | `config_users.py`、`config_teams.py`、`config_invites.py` |
| 数据监控 | 用户调用日志 / Token 计费 / 用户聊天记录 / 工具・子智能体・技能调用日志 | `admin_usage_logs.py`、`admin_billing.py`、`admin_chat_history.py`、`admin_logs.py` |
| 安全管理 | 沙盒管理 / 审计日志 / 系统健康 | `config_security.py` |
| 授权管理 | License | `config_license.py` |

> 命名提示：`admin_prompts`、`admin_mcp_servers`、`admin_billing` 等路由文件虽以 `admin_` 命名，但鉴权依赖是 `require_config`（`CONFIG_TOKEN`），对应面板渲染在 `/config`——文件名前缀不代表凭证归属。

## 后端管理路由分组

### 技能管理（admin_skills / admin_skill_drafts）（商业版 EE：content_admin）

`/v1/admin/skills`（`api/routes/v1/admin_skills.py`，`ADMIN_TOKEN`）：技能全生命周期管理——CRUD、zip 上传、启停、排序、图标、依赖扫描与编辑、技能内单文件读写删、复刻内置技能、导入 / 导出。技能存于 `admin_skills` 表，技能体系详见 [Agent 技能](agent-skills.md)。

`/v1/admin/skill-drafts`（`api/routes/v1/admin_skill_drafts.py`，`ADMIN_TOKEN`）：自动蒸馏出的技能草稿审核——列表 / 计数 / 详情、先编辑后通过（approve）、驳回（reject）、删除，以及手动触发每日蒸馏扫描。

### 市场审核（admin_marketplace）（商业版 EE：content_admin）

`/v1/admin/marketplace`（`api/routes/v1/admin_marketplace.py`，`ADMIN_TOKEN`）：两件事——

1. **安装市场技能为全局技能**：浏览市场列表（标注「是否已全局安装」）、安装后 owner 为空、全员可用，可继续在「技能管理」编辑。
2. **审核用户上架申请**：申请列表（按 status 过滤）、详情（含 SKILL.md 预览）、通过（上架，全员可装）、驳回 / 下架。

### 提示词管理（admin_prompts）（商业版 EE：content_admin）

`/v1/admin/prompts`（`api/routes/v1/admin_prompts.py`，`CONFIG_TOKEN`）分两层：

- **分段（parts）**：系统提示词按有序 `.md` 分段管理——列表 / 详情 / 保存 / 删除 / 排序 / 运行时预览。DB 记录覆盖文件系统兜底文件，删除 DB 记录即还原文件版本。
- **版本池（versions）**：system / code_exec / distillation / plan_mode 四类提示词的多版本管理——CRUD、激活（activate）、从文件系统初始化（seed）。存储在 `ContentBlock(id="prompt_versions")`。

另有跨环境迁移快照接口 `GET /v1/content/prompts/export` / `POST /v1/content/prompts/import`（`content.py`，`ADMIN_TOKEN` 或 `CONFIG_TOKEN` 皆可）。详见 [提示词系统](prompts.md)。

### MCP 管理（admin_mcp_servers）（商业版 EE：content_admin）

`/v1/admin/mcp-servers`（`api/routes/v1/admin_mcp_servers.py`，`CONFIG_TOKEN`）：MCP 服务器配置 CRUD（存 `admin_mcp_servers` 表）、启停、连通性测试（test）、连接池重载（reload-pool）。详见 [MCP 工具](mcp-tools.md)。

### 子智能体（admin_agents）（商业版 EE：content_admin）

`/v1/admin/agents`（`api/routes/v1/admin_agents.py`，`ADMIN_TOKEN`）：管理员侧子智能体 CRUD（全员可见）、可绑定资源列表（available-resources）、启停切换、导入 / 导出。

### 知识库管理（admin_kb）（商业版 EE：content_admin）

`/v1/admin/kb`（`api/routes/v1/admin_kb.py`，`ADMIN_TOKEN`）：自建**公共知识库**管理（`KBSpace.visibility == "public"`，归属系统属主），能力镜像用户态 `kb.py`——库 CRUD、AI 生成简介、文档上传 / 列表 / 原文预览（Office 转 PDF）/ 删除 / 重新索引、分块预览与逐块编辑（内容 / 标签 / 问题）。公共库对全部用户在能力目录可见、可被检索。Dify 模式下仅只读展示 Dify 数据集，写操作返回 409。详见 [知识库](knowledge-base.md)。

### 沙箱管理（admin_sandbox + config_security/sandbox）

两个互补面板：

- `/v1/admin/sandbox`（`api/routes/v1/admin_sandbox.py`，`ADMIN_TOKEN`，**商业版 EE：content_admin**）：聚合技能依赖清单预览、触发沙盒镜像重建 + 容器热切、重建历史与日志查询。
- `/v1/config/security/sandbox/*`（`api/routes/v1/config_security.py`，`CONFIG_TOKEN`，**商业版 EE：system_config**）：只读安全视图——沙盒概览、运行实例列表 / 详情、快照、重建历史、脱敏配置。Provider 不支持的能力（如 ScriptRunner 无法枚举实例）返回 `code=42210`，前端置灰。

详见 [沙箱](sandbox.md)。

### 计费报表（admin_billing / admin_usage_logs）（商业版 EE：billing）

`/v1/admin/billing`（`api/routes/v1/admin_billing.py`，`CONFIG_TOKEN`）：计费汇总统计、按模型定价（pricing CRUD）、计费明细 CSV 导出。

`/v1/admin/usage-logs`（`api/routes/v1/admin_usage_logs.py`，`CONFIG_TOKEN`）：按用户的智能体调用日志查询（token 用量、模型、错误状态）、统计汇总、去重模型名列表。

> 社区版用户可查看自己的 Token 用量；面向组织的汇总报表 / 定价 / 成本导出归商业版。

### 日志与会话审查（admin_logs / admin_chat_history）（商业版 EE：audit）

`/v1/admin/logs`（`api/routes/v1/admin_logs.py`，`CONFIG_TOKEN`）：可观测性日志三件套——工具调用、子智能体调用、技能调用日志，各含列表 / 筛选项 / 统计 / 详情，外加 `GET /trace/{trace_id}` 按 trace 聚合完整调用链。

`/v1/admin/chat-history`（`api/routes/v1/admin_chat_history.py`，`CONFIG_TOKEN`）：全量用户会话浏览、消息明细（含工具调用结果）、用户筛选、会话历史 XLSX 导出。

另有面向 API 的全局审计查询 `/v1/audit`（`api/routes/v1/audit.py`，**商业版 EE：audit**）：审计日志查询 / 详情 / CSV / JSON 导出 / 统计；`/config` 的「审计日志」面板走 `config_security.py` 的 `/v1/config/security/audit-logs*`。

### 内容管理（content.py）（社区版 CE）

`/v1/content`（`api/routes/v1/content.py`）是少数留在 CE 的管理路由——品牌、文案、版本说明属于「可改品牌」的开源体验：

| 端点 | 凭证 | 说明 |
|---|---|---|
| `GET /docs`、`GET /docs/version` | 公开读 | 前台读取内容块 / 轻量轮询版本 |
| `PUT /docs/{block_id}` | `ADMIN_TOKEN` | 写内容块：`docs_updates`（功能更新时间轴）、`docs_capabilities`（能力中心）、`prompt_hub`（提示词广场） |
| `POST /manual/upload`、`GET /manual` | `ADMIN_TOKEN` 写 | 操作手册 PDF |
| `PUT /app_config`、`PUT /homepage_shortcuts`、`PUT /page_config`、`POST /page_config/assets/upload` | `CONFIG_TOKEN` | 应用配置 / 首页快捷方式 / 页面品牌（logo、导航、文案） |
| `GET/POST /docs/export|import`、`GET/POST /prompts/export|import` | 管理凭证 | 内容 / 提示词快照迁移 |

### 用户 / 团队 / 邀请 / 安全 / License 管理

| 路由组 | 文件 | 凭证 | 版本 | 说明 |
|---|---|---|---|---|
| `/v1/config/users` | `config_users.py` | `CONFIG_TOKEN` | 商业版 EE（multi_tenancy） | 用户列表 / 详情 / 状态、重置密码、删除，及五个权限位（应用可见范围、实验室、API-Key、自助技能、自助 MCP），见 [认证与权限](auth.md) |
| `/v1/config/teams` | `config_teams.py` | `CONFIG_TOKEN` | 商业版 EE（multi_tenancy） | 团队 CRUD、成员增删、角色设置 |
| `/v1/config/invites` | `config_invites.py` | `CONFIG_TOKEN` | 商业版 EE（multi_tenancy） | 注册码批量生成 / 列表 / 吊销 / 删除 |
| `/v1/config/security` | `config_security.py` | `CONFIG_TOKEN` | 商业版 EE（system_config） | 安全管理台（只读）：沙盒、审计日志、系统健康快照 |
| `/v1/config/license` | `config_license.py` | `CONFIG_TOKEN` | 豁免 feature 守卫 | 查看 License 状态、上传激活新 License——**License 失效时也必须可达**，否则无法换证，见 [License](../editions/license.md) |
| `/v1/config/verify` | `config_verify.py` | `CONFIG_TOKEN` | 豁免 | 管理台登录前校验令牌有效性 |

### 服务配置（service_configs）（商业版 EE：system_config）

`/v1/service-configs`（`api/routes/v1/service_configs.py`，`CONFIG_TOKEN`）：外部服务配置中心——query_database、knowledge_base、industry、file_parser 四个分组的列表 / 批量更新 / 连通性测试 / 导入导出。全部端点都是管理写操作与探测，无公开读，整体归 EE。

## CE / EE 边界小结

按《开源与商业化产品方案》第四章与路由注册表对齐：

- **社区版（CE）保留**：`content.py` 内容块管理（品牌可定制）、`models.py` 模型管理、登录基础设施（`auth.py` 会话端点、mock SSO）。
- **商业版（EE）**：完整内容管理台（技能 / 草稿 / 市场 / 知识库 / 子智能体 / 提示词 / MCP / 沙盒依赖，`content_admin`）、系统管理台（服务配置 + 安全台，`system_config`）、审计与会话审查（`audit`）、团队计费与用量（`billing`）、用户 / 团队 / 注册码（`multi_tenancy`）。
- `config_license`、`config_verify`、`auth` 显式豁免 License 守卫，保证「402 → 换证」的逃生通道始终可达。

## 相关源码

| 主题 | 路径 |
|---|---|
| 前端入口分流 | `src/frontend/src/main.tsx` |
| 运营管理台 | `src/frontend/src/AdminApp.tsx`、`src/frontend/src/components/admin/` |
| 系统管理台 | `src/frontend/src/ConfigApp.tsx`、`src/frontend/src/components/config/` |
| 路由注册表（CE/EE 单一真源） | `src/backend/api/routes/v1/__init__.py` |
| 管理凭证依赖 | `src/backend/api/deps.py` |
| License 能力位 | `src/backend/edition_ee/licensing/features.py`、`src/backend/edition_ee/licensing/deps.py` |
| 技能 / 草稿 / 市场 | `src/backend/api/routes/v1/admin_skills.py`、`admin_skill_drafts.py`、`admin_marketplace.py` |
| 提示词 / MCP / 子智能体 | `src/backend/api/routes/v1/admin_prompts.py`、`admin_mcp_servers.py`、`admin_agents.py` |
| 知识库 / 沙盒 | `src/backend/edition_ee/routes/admin_kb.py`、`src/backend/api/routes/v1/admin_sandbox.py` |
| 计费 / 用量 / 日志 / 会话审查 | `src/backend/api/routes/v1/admin_billing.py`、`admin_usage_logs.py`、`admin_logs.py`、`admin_chat_history.py` |
| 内容管理 | `src/backend/api/routes/v1/content.py` |
| 用户 / 团队 / 邀请 / License（EE） | `src/backend/edition_ee/routes/config_users.py`、`config_teams.py`、`config_invites.py`、`config_license.py` |
| 安全 | `src/backend/api/routes/v1/config_security.py` |
| 服务配置 | `src/backend/api/routes/v1/service_configs.py` |

延伸阅读：[认证与权限](auth.md) · [提示词系统](prompts.md) · [版本与授权](../editions/overview.md)
