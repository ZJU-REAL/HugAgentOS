# 认证与权限

> 最后更新：2026-07-20

HugAgentOS 的认证与权限体系覆盖三条互相独立的链路：**终端用户认证**（本地账号 / Mock SSO / 企业 SSO / 个人 API-Key）、**管理凭证**（`ADMIN_TOKEN` 与 `CONFIG_TOKEN` 两类 Bearer 令牌）以及**资源级权限**（团队文件、项目、会话分享的细粒度访问控制）。整套实现集中在 `src/backend/core/auth/`，FastAPI 依赖注入入口在 `src/backend/api/deps.py`。

社区版（CE）以单用户 / 单租户为中心；团队、邀请码、企业 SSO 等组织级能力属**商业版（EE）**，通过路由注册表（`api/routes/v1/__init__.py`）与 License 能力位双重门控，详见 [版本与授权](../editions/overview.md)。

## 认证模式（AUTH_MODE）

`AUTH_MODE` 环境变量决定后端如何校验请求身份，由 `core/auth/backend.py` 实现。实际支持**三种**模式（而非常见文档所写的两种）：

| 模式 | 适用场景 | 校验方式 |
|---|---|---|
| `mock`（默认） | 本地开发 | 任意 Bearer token 即视为该用户名登录；无 token 时注入默认开发用户（`AUTH_MOCK_USER_ID` / `AUTH_MOCK_USERNAME`） |
| `remote` | 对接外部用户中心（历史模式） | 携带 Bearer token 调用 `{AUTH_API_URL}/verify` 远端校验，带重试（`AUTH_RETRY_COUNT`）与超时（`AUTH_API_TIMEOUT`） |
| `session` | 生产主路径（SSO 票据登录） | 校验 `jx_session` Cookie 对应的 Redis 会话；**不接受**普通 Bearer token |

`get_current_user`（`core/auth/backend.py`）的判定优先级在所有模式下一致：

1. **Cookie 会话**（`jx_session` → Redis 查询）——任何模式下优先；Cookie 存在但会话过期时直接返回 401（code `30003`），不降级。
2. **个人 API-Key**（`sk-jx-` 前缀 Bearer）——所有模式下有效；形似 API-Key 但校验失败时一律 401，绝不静默降级为匿名。
3. **Bearer token**——`session` 模式下拒绝；`mock` / `remote` 模式下分别走 mock 校验 / 远端 `verify`。

401 响应统一携带 `login_url`（由 `settings.sso.effective_login_url` 按部署模式自动推导），前端据此跳转登录页。

## 登录方式

### 本地账号（社区版默认）

`LOCAL_AUTH_ENABLED=true`（默认开启）时，`api/routes/v1/mock_sso.py` 中的 `login_router` 提供统一登录页。全新 CE 数据库只会创建一个本地管理员：默认账号和初始密码均为 `admin`，首次登录必须立即修改密码。如果一键安装已通过引导创建了自定义管理员，则保留该唯一账号，不再追加 `admin`。

- `GET /login` — CE 仅渲染登录表单，不显示注册入口
- `POST /login` — 账号密码登录提交
- `POST /register` — CE 始终拒绝自助注册；其他版本可由 `page_config.auth.allow_register` 控制

密码哈希在 `core/auth/password.py`，最小长度由 `PASSWORD_MIN_LENGTH`（默认 8）控制。本地账号存储于 `local_users` 表（`core/db/models` 的 `LocalUser`），与影子用户表一对一关联。CE 同时关闭已知 Mock 账号和 `?auto=` 免密捷径；它们只能在非 CE 且明确设置 `SSO_LOGIN_MODE=mock` 的开发环境使用。

### CE 首次初始化

使用默认 `admin/admin` 登录的 CE 实例在修改临时密码后，会进入全屏首次初始化向导，
而不是直接进入工作台。向导依次设置界面语言、主对话模型、互联网搜索、文件解析、
永久记忆和本体核验。

- 主对话模型是完成向导的必填项。保存时后端会实测连通性，并把模型指派给全部
  对话角色；没有已启用的 `main_agent` 模型时，完成接口会拒绝解锁工作台。
- 互联网搜索和外部文件解析均可跳过，以后可在**设置 → 系统管理**中补充。
- 记忆和本体开关会先检查实例是否具备对应服务或已发布领域包；不可用时保持关闭，
  不会制造“已开启但运行时无效”的状态。
- 完成状态存放在当前管理员的 `users_shadow.metadata` 中，并带版本号。刷新或重新登录
  后不会重复进入已完成的向导。

一键安装器的终端 `hugagent onboard` 已经完成账号和模型配置，因此成功结束时会写入
同一个完成标记，不会再重复显示网页向导。

### Mock SSO（开发联调）

`SSO_MOCK_ENABLED=true` 时注册 `/mock-sso/*` 路由（`api/routes/v1/mock_sso.py`），模拟外部统一登录系统的完整票据流：

```
GET  /mock-sso/login            → 生成一次性 ticket 并带 ?ticket=... 重定向回应用
POST /mock-sso/ticket/exchange  → 校验 ticket，返回用户信息
```

票据存储在进程内的 `core/auth/mock_ticket_store.py`，供 `core/auth/sso.py` 在 mock 模式下消费。手工验证流程：浏览器打开 `http://localhost:3001/mock-sso/login?redirect=/`。

### 企业 SSO（商业版 EE）

生产 SSO 走 ticket 交换流程，由 `core/auth/sso.py` 实现，**受 License 能力位 `Feature.SSO` 门控**：

- `POST /v1/auth/ticket/exchange` — 用一次性 ticket 换取会话（`api/routes/v1/auth.py`）
- `GET /v1/auth/sso/authorize-url` — 代理 SSO 提供方的 OAuth 授权地址，路由级挂 `requires_feature(Feature.SSO)`
- `GET /v1/auth/session/check` / `POST /v1/auth/logout` — 会话检查与注销（基础设施，不受 License 限制）

`SSO_EXCHANGE_MODE` 区分真实模式（`GET {SSO_TICKET_EXCHANGE_URL}?{callback_param}={credential}`）与 mock 模式；`SSO_CALLBACK_PARAM` 支持 `ticket`（旧版）与 `code`（OAuth2 风格的省级新版 SSO）两种参数名。

### 会话管理

`core/auth/session.py` 负责会话生命周期：

- Redis Key 格式 `jx:session:{sha256(token)}`，值为 JSON 用户数据
- TTL 由 `SESSION_TTL_HOURS` 控制（默认 8 小时）
- `SESSION_STORE_TYPE=memory` 时可退化为进程内字典（无 Redis 的最小部署）

> CE 派生树物理不含 `core/auth/session.py` 时，`backend.py` 的 Cookie 解析会安静短路到 Bearer / mock 路径（接缝 C5）。

## 用户体系

外部身份（SSO / 用户中心）与本地业务数据通过**影子用户表** `users_shadow`（ORM 模型 `UserShadow`）解耦：首次认证成功时 `UserService.get_or_create_user_shadow()` 落库，后续所有业务表（会话、文件、记忆等）以 `user_id` 外键关联。

用户侧接口：

| 接口 | 文件 | 说明 |
|---|---|---|
| `GET/PATCH /v1/me` | `api/routes/v1/users.py` | 个人资料（含部门、团队、本地账号信息）；本地账号可改昵称 / 真名 / 手机号 |
| `POST/PUT/DELETE /v1/me/avatar` | `api/routes/v1/users.py` | 头像上传（≤2MB）/ 设置 / 清除 |
| `GET/PUT /v1/users/{id}/preferences` | `api/routes/v1/users.py` | 用户偏好设置 |
| `POST /v1/me/onboarding/complete` | `api/routes/v1/users.py` | 校验主模型并完成 CE 首次初始化 |
| `GET /v1/me/teams` 等 | `api/routes/v1/me.py` | 用户侧团队查看、邀请成员、移除 / 退出（商业版 EE；CE 树缺团队模块时降级 404） |
| `GET /v1/me/users/search` | `api/routes/v1/me.py` | 邀请成员时的用户搜索 |

## 权限体系

### 接口层（CE/EE 拆分接缝）

`core/auth/permissions_iface.py` 是权限符号的**唯一导入入口**：主干代码（deps / files / chats / projects / kb 等）一律从这里导入，不直接引用具体实现。EE 主仓中它纯转发三个真实现；CE 派生树用单租户 stub 整体替换本文件，三个真实现物理不存在：

| 实现文件 | 职责 |
|---|---|
| `core/auth/team_permissions.py` | 团队文件夹权限解析（商业版 EE） |
| `core/auth/project_permissions.py` | 项目访问权限（团队项目属商业版 EE） |
| `core/auth/chat_share_permissions.py` | 会话访问 / 删除 / 分享范围权限 |

`resolve_artifact_access(db, user_id, owner_id, team_id)` 是 owner ∪ team 合成的统一访问级判定：owner 恒为 `admin` → 团队成员按团队权限 → 其余 `none`。文件下载（`api/routes/files.py`）、知识库、我的空间等所有 artifact 访问点共用。

### 团队角色与文件权限（商业版 EE）

`core/auth/roles.py` 定义三级团队角色：`owner`（所有者）> `admin`（管理员）> `member`（成员）。团队文件权限映射（`team_permissions.py`）：

| 团队角色 | 文件权限 | 能做什么 |
|---|---|---|
| owner / admin | `admin` | 全操作：上传 / 删除任何文件、管理文件夹、配置成员权限 |
| member + editor | `edit` | 上传、删除自己上传的文件、从个人空间移入团队 |
| member + viewer | `view` | 只读 |
| 非成员 | `none` | 无访问 |

路由层通过 `api/deps.py` 的依赖工厂消费：`require_team_role(min_role)`、`require_team_file_perm(min_permission)`（产出 `TeamFileAccess` 上下文）。所有拒绝都会经 `AuditLogRepository.log_denial()` 写入审计日志。

### 用户级权限位

按用户粒度的功能开关存储在 `users_shadow.metadata`（ORM 字段 `extra_data`）JSON 列中，由 Config 管理台的用户管理模块（`api/routes/v1/config_users.py`）设置。**默认全部关闭**（关闭即从 metadata 移除键）：

| 权限位 | 默认 | 控制接口 | 门控内容 |
|---|---|---|---|
| `can_use_api_key` | 关 | `PATCH /v1/config/users/{id}/api-key-permission` | 是否可创建 / 使用个人 API-Key；关闭后已有 Key 立即失效 |
| `can_add_skill` | 关 | `PATCH /v1/config/users/{id}/skill-permission` | 能力中心自助上传 / 手写私有技能（`api/routes/v1/me_capabilities.py`） |
| `can_add_mcp` | 关 | `PATCH /v1/config/users/{id}/mcp-permission` | 能力中心自助添加私有远程 MCP（HTTP/SSE） |
| `lab_enabled` | **开** | `PATCH /v1/config/users/{id}/lab-permission` | 实验室模块入口与访问 |
| `allowed_apps` | 无限制 | `PATCH /v1/config/users/{id}/app-permissions` | 应用可见范围；`None`=全部启用应用，列表=白名单（空列表=全屏蔽） |
| `role: super_admin` | 无 | （直接写 metadata） | `require_super_admin` 依赖放行；可绕过团队角色检查 |

自助添加的私有 MCP / 技能记录 `owner_user_id` = 当前用户，**仅本人可见可用**（owner 隔离）。

## 管理凭证：ADMIN_TOKEN 与 CONFIG_TOKEN

平台有两类互相独立的管理 Bearer 令牌，分别对应 `/admin`（运营管理台）与 `/config`（系统管理台）两个前端入口。依赖实现是 `api/deps.py` 的 `_require_token` 工厂，产出 `require_admin` 与 `require_config`；未配置对应环境变量时直接 503，token 不匹配时 401 并写审计（仅记录「带了 header 但 token 错误」的疑似攻击，不记录裸探测以防审计表被 DoS 放大）。

实际门控范围（按各路由文件的依赖逐一核实）：

| 凭证 | 门控的路由组 |
|---|---|
| `ADMIN_TOKEN`（`require_admin`） | `admin_skills`（技能管理）、`admin_skill_drafts`（蒸馏草稿审核）、`admin_marketplace`（市场上架审核）、`admin_kb`（公共知识库管理）、`admin_sandbox`（沙盒依赖重建）、`admin_agents`（子智能体）、`content.py` 中的内容块写入（如 `PUT /v1/content/docs/{block_id}`、操作手册上传） |
| `CONFIG_TOKEN`（`require_config`） | `admin_prompts`（提示词管理）、`admin_mcp_servers`（MCP 管理）、`admin_billing` / `admin_usage_logs`（计费与用量）、`admin_logs` / `admin_chat_history`（调用日志与会话审查）、`config_users` / `config_teams` / `config_invites`（用户 / 团队 / 注册码）、`config_security`（安全管理台）、`config_license`（License）、`config_verify`（令牌校验）、`service_configs`（外部服务配置）、`models.py`（模型管理）、`content.py` 中的页面 / 应用配置写入 |
| 两者皆可（`require_admin_or_config`） | 提示词快照导入 / 导出（`/v1/content/prompts/export|import`）——CLI 迁移脚本带 `ADMIN_TOKEN`，Config 后台带 `CONFIG_TOKEN` |
| `require_super_admin` | 会话用户 metadata 中 `role=super_admin`，或合法 `ADMIN_TOKEN` 作为 fallback |

> 注意命名与凭证并不一一对应：`admin_prompts`、`admin_mcp_servers`、`admin_billing` 等虽以 `admin_` 命名，实际由 `CONFIG_TOKEN` 门控，对应面板也渲染在 `/config` 系统管理台。完整面板划分见 [管理台](admin-console.md)。

## 个人 API-Key

`api/routes/v1/api_keys.py` 提供个人 API-Key 全生命周期管理，前提是用户权限位 `can_use_api_key=true`（否则 403）：

| 接口 | 说明 |
|---|---|
| `GET /v1/me/api-keys` | 列出当前用户的 Key |
| `POST /v1/me/api-keys` | 新建——**明文仅在创建响应中返回一次**；过期时间可选 7/30/90/180/365 天或永不过期 |
| `PATCH /v1/me/api-keys/{key_id}` | 启用 / 禁用 |
| `DELETE /v1/me/api-keys/{key_id}` | 撤销 |

Key 形如 `sk-jx-...`，在**所有 AUTH_MODE 下**都可作为 Bearer 调用业务 API（外部程序化调用通常没有 Cookie）。校验逻辑（启用 / 未撤销 / 未过期 / 用户权限位仍开启）集中在 `core/services/api_key_service.py::resolve_api_key`。

## 团队与邀请码（商业版 EE）

团队与注册码属多租户能力（License 能力位 `multi_tenancy`），管理端在 Config 系统管理台：

- **团队管理**（`api/routes/v1/config_teams.py`）：团队 CRUD、成员增删、角色设置（owner/admin/member）。
- **注册码管理**（`api/routes/v1/config_invites.py`）：批量生成、列表、吊销、删除。注册码形如 `JX-ABCD-2345`（`core/auth/invite.py`，字符表去除易混淆的 O/0、I/1 等），默认有效期 `INVITE_CODE_DEFAULT_TTL_HOURS`（168 小时）；消费用条件 UPDATE 保证并发安全，可预绑定团队与角色。
- **用户侧**（`api/routes/v1/me.py`）：团队 owner/admin 可直接邀请成员、移除成员；成员可退出。

## 审计

认证链路的关键事件全部落审计表（`audit_logs`）：登录成功 / 失败（`auth.login.*`）、管理令牌错误尝试（`admin.access_denied` / `config.access_denied`）、团队 / 文件 / super_admin 权限拒绝（`*.access_denied`，含所需权限与实际权限）。审计查询台属商业版 EE，见 [管理台](admin-console.md)。

## 相关源码

| 主题 | 路径 |
|---|---|
| 认证后端（三种模式 + 判定优先级） | `src/backend/core/auth/backend.py` |
| 会话管理（Redis） | `src/backend/core/auth/session.py` |
| SSO 票据交换 | `src/backend/core/auth/sso.py`、`src/backend/api/routes/v1/auth.py` |
| Mock SSO / 本地登录注册页 | `src/backend/api/routes/v1/mock_sso.py`、`src/backend/core/auth/mock_ticket_store.py` |
| 密码哈希 | `src/backend/core/auth/password.py` |
| 权限接口层（CE/EE 接缝） | `src/backend/core/auth/permissions_iface.py` |
| 团队角色 / 文件权限 | `src/backend/core/auth/roles.py`、`src/backend/core/auth/team_permissions.py` |
| 项目 / 会话分享权限 | `src/backend/core/auth/project_permissions.py`、`src/backend/core/auth/chat_share_permissions.py` |
| 管理凭证依赖 | `src/backend/api/deps.py` |
| 用户资料 / 偏好 | `src/backend/api/routes/v1/users.py`、`src/backend/api/routes/v1/me.py` |
| 用户权限位管理 | `src/backend/api/routes/v1/config_users.py` |
| 个人 API-Key | `src/backend/api/routes/v1/api_keys.py`、`src/backend/core/services/api_key_service.py` |
| 能力中心自助（owner 隔离） | `src/backend/api/routes/v1/me_capabilities.py` |
| 邀请码 | `src/backend/core/auth/invite.py`、`src/backend/api/routes/v1/config_invites.py` |
| 团队管理 | `src/backend/api/routes/v1/config_teams.py` |
| License 能力位守卫 | `src/backend/core/licensing/features.py`、`src/backend/core/licensing/deps.py` |

延伸阅读：[管理台](admin-console.md) · [版本与授权](../editions/overview.md) · [环境变量](../deployment/environment-variables.md)
