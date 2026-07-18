# 项目空间与我的空间

> 最后更新：2026-06-11

HugAgentOS 提供两级个人化工作区：

- **我的空间（MySpace）**：用户的文件资产中枢——上传文件、AI 会话产物、个人文件夹树、会话收藏、分享记录与消息通知；
- **项目（Projects）**：工作空间——把一组文件（挂钩文件夹）+ 一段项目指令（instructions）+ 独立记忆作用域组合起来，在项目内发起的所有对话自动携带这些上下文。项目分 `personal` / `team` 两种（团队项目依赖团队体系，属商业版 EE）。

二者通过**文件夹强挂钩**打通：项目不是独立的文件容器，而是直接挂在我的空间（或团队空间）的某个文件夹上，项目文件操作本质就是该文件夹下的 artifact 操作。

## 数据模型

```
users_shadow ──┬── user_folders（个人文件夹树，NULL parent = 根）
               ├── artifacts（文件资产；user_folder_id 定位个人文件夹）
               └── projects（kind=personal，linked_folder_id → user_folders）

teams ─────────┬── team_members（role: owner/admin/member + file_permission: viewer/editor）
（商业版 EE）   ├── team_folders（团队文件夹树）
               ├── artifacts（team_id + team_folder_id 非空即团队文件）
               └── projects（kind=team，linked_team_folder_id → team_folders）
```

关键 ORM（`src/backend/core/db/models/`）：

| 模型 | 表 | 要点 |
|---|---|---|
| `Project` | `projects` | `kind`（personal/team）、`instructions`、`linked_folder_id` / `linked_team_folder_id` 互斥挂钩、`pinned`、`metadata`（含项目级记忆开关）；CHECK 约束保证 kind 与 team_id 匹配 |
| `ProjectFavorite` | `project_favorites` | 每人独立 star，不影响他人视图 |
| `UserFolder` | `user_folders` | 个人文件夹树；命名安全约束（禁 `/`、`.`、`..`） |
| `Artifact` | `artifacts` | 文件本体：`storage_key`（对象存储）、`user_folder_id` 与 `team_folder_id` 互斥、`parsed_text` / `summary` 跨轮读取缓存、软删 `deleted_at` |
| `Team` / `TeamMember` | `teams` / `team_members` | 团队与成员（商业版 EE）；可由外部 SSO 部门自动建立（`source=sso_auto`） |
| `TeamFolder` | `team_folders` | 团队文件夹树（商业版 EE） |

## 项目（Projects）

路由：`src/backend/api/routes/v1/projects.py`（CE 路由表），业务在 `core/services/project_service.py` 与 `project_file_service.py`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/projects` | 列表（个人 + 可见团队项目混合） |
| POST | `/v1/projects` | 创建（`kind=personal\|team`；可指定或自动新建挂钩文件夹） |
| GET | `/v1/projects/teams` | 可创建团队项目的团队列表 |
| GET / PATCH / DELETE | `/v1/projects/{id}` | 详情 / 改名、描述、pin、icon、instructions / 软删 |
| POST / DELETE | `/v1/projects/{id}/favorite` | star / 取消 |
| GET | `/v1/projects/{id}/files` | 项目文件列表（递归挂钩文件夹子树） |
| POST | `/v1/projects/{id}/files/upload` | 直传（`filename` 可含路径，自动建子文件夹） |
| DELETE | `/v1/projects/{id}/files/{artifact_id}` | 软删（同步我的空间） |
| PATCH | `/v1/projects/{id}/instructions` | 更新项目指令 |
| GET | `/v1/projects/{id}/chats` | 项目内会话列表（团队项目可见共享会话） |

### 项目上下文如何进入对话

在项目内发起对话时（请求携带 `project_id`），`api/routes/v1/chats.py` 组装 workflow context：

1. 读取项目元信息——`project_name`、`project_instructions`、挂钩文件夹名与文件清单；
2. `core/llm/agent_factory.py` 把这些经 `_build_project_section` 注入 system prompt（`build_system_prompt(cfg, ctx=...)`）；
3. 项目级记忆作用域随之生效：workspace 为 `project:<project_id>`，团队项目的 mem0 桶为 `team:<team_id>`（成员共享记忆），项目自身的 `metadata.memory_enabled` / `memory_write_enabled` 覆盖用户级开关（项目内缺省开启）——详见 [记忆系统](./memory.md)；
4. 沙箱侧路径作用域：项目对话中 agent 的 `/myspace/...` 文件操作被重定向到挂钩文件夹之下（`core/llm/tools/myspace_vfs.py` 的 `ProjectScope` 显式传参机制）。

团队项目权限沿用团队角色：owner/admin 恒为管理权限，member 按 `file_permission`（editor/viewer）二级控制（`core/auth/permissions_iface.py::require_project_access`）。

## 我的空间（MySpace）

### 文件资产与个人文件夹

- 资产列表：`GET /v1/artifacts`（`api/routes/v1/artifacts.py`），支持按类型 / 来源 / 文件夹过滤；
- 删除：`DELETE /v1/artifacts/{artifact_id}`（软删）；
- 加入知识库：`POST /v1/artifacts/{artifact_id}/knowledge-base`（配合系统托管的「我的空间同步知识库」，见 [知识库](./knowledge-base.md)）；
- 个人文件夹树：`src/backend/api/routes/v1/myspace_folders.py`——

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/myspace/folders` | 文件夹列表 |
| GET | `/v1/myspace/folders/breadcrumb` | 面包屑路径 |
| POST | `/v1/myspace/folders` | 创建 |
| PATCH / DELETE | `/v1/myspace/folders/{folder_id}` | 重命名、移动 / 级联删除（带影响数预检端点） |
| POST | `/v1/myspace/folders/move-artifact` | 移动文件到文件夹 |

文件上传统一走 `POST /v1/file/upload`（可带 `folder_id` 落入指定文件夹），存储链路见 [对象存储](./storage.md)。

### 会话收藏（favorites）

「收藏」收藏的是**会话**：`ChatSession.favorite` 标记，`GET /v1/artifacts/favorites` 返回收藏的会话列表（`api/routes/v1/artifacts.py`）。项目则有独立的 `project_favorites` star 机制。

### 前端

`src/frontend/src/components/myspace/MySpacePanel.tsx` 四个 Tab：**文件资产**（assets）、**会话收藏**（favorites）、**分享记录**（shares）、**消息通知**（notifications）。子组件：

- `DocumentList.tsx` / `ImageGrid.tsx` / `FavoriteList.tsx` / `NotificationList.tsx` / `ResourceCard.tsx`；
- `personal/`：个人文件夹创建与移动弹窗；
- `team/`：团队作用域树、面包屑、移动到团队、权限管理弹窗（商业版 EE）；
- 状态：`stores/mySpaceStore.ts`。

项目前端在 `src/frontend/src/components/projects/`：`ProjectsPanel`（列表）、`ProjectCard`、`CreateProjectModal`、`ProjectDetailPanel`（文件 + 指令 + 会话）、`ProjectRightRail`、`ProjectMemoriesModal`（项目记忆查看）；状态在 `stores/projectStore.ts`。

## 团队文件夹与团队文件（商业版 EE）

用户侧路由 `src/backend/api/routes/v1/team_files.py`，挂 `multi_tenancy` 能力位（EE 路由表）；管理台对应 `/v1/config/teams/*`（`config_teams.py`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/my-teams` | 我所在的团队 |
| GET / POST | `/v1/teams/{team_id}/folders` | 团队文件夹列表 / 创建 |
| PATCH / DELETE | `/v1/teams/{team_id}/folders/{folder_id}` | 重命名 / 删除 |
| GET / POST | `/v1/teams/{team_id}/files[, /upload]` | 团队文件列表 / 上传 |
| DELETE / POST | `/v1/teams/{team_id}/files/{artifact_id}[, /move]` | 删除 / 移动 |
| POST | `/v1/artifacts/{artifact_id}/move-to-team` | 个人文件转团队文件 |
| GET / PUT | `/v1/teams/{team_id}/members/permissions`、`.../{user_id}/permission` | 成员文件权限查看 / 调整 |

权限模型：`TeamMember.role`（owner/admin/member）+ `file_permission`（viewer/editor，仅对 member 生效），鉴权封装在 `core/auth/team_permissions.py`。团队文件在沙箱侧有独立共享缓存 `team_cache_dir(team_id)`，同团队成员复用一份镜像。

## 文件如何进入对话上下文

三条路径，互为补充：

1. **附件注入（hooks）**：用户在输入框上传 / 从我的空间选择文件后，请求 `attachments[].file_id` 经 `core/llm/hooks.py` 的 pre_reply hook 处理——`_build_file_context()` 拉取 `parsed_text`（缺失时从对象存储下载并解析），拼成上下文文本注入；单文件 50K 字符预算，超出截断并提示用 `read_artifact` 分页续读；xlsx 走专用「摘要 + 预览 + 操作指引」分支防止截断误导；图片走多模态注入分支。所有按 `file_id` 的拉取都校验归属 `user_id`，防伪造跨用户读取；
2. **项目文件清单**：项目对话的 system prompt 携带挂钩文件夹的文件列表（见上文），agent 按需用 `read_artifact` / 沙箱工具读取具体内容；
3. **沙箱虚拟文件系统**：开启代码执行时，`/myspace/...` 路径把我的空间映射进沙箱（懒加载 + 反向同步），团队项目映射团队文件夹——见 [沙箱](./sandbox.md)。

## 相关源码

| 路径 | 职责 |
|---|---|
| `src/backend/api/routes/v1/projects.py` | 项目 API |
| `src/backend/core/services/project_service.py` / `project_file_service.py` | 项目业务逻辑 |
| `src/backend/core/services/project_scope.py` | `ProjectScope`（沙箱路径作用域） |
| `src/backend/api/routes/v1/myspace_folders.py` | 个人文件夹 API |
| `src/backend/api/routes/v1/artifacts.py` | 资产列表 / 会话收藏 / 加入知识库 |
| `src/backend/api/routes/v1/team_files.py` | 团队文件夹与文件 API（商业版 EE） |
| `src/backend/api/routes/v1/file_upload.py` | 文件上传（可指定文件夹） |
| `src/backend/core/db/models/project.py` | `Project` / `ProjectFavorite` ORM |
| `src/backend/core/db/models/identity.py` | `Team` / `TeamMember` / `TeamFolder` / `UserFolder` ORM |
| `src/backend/core/db/models/artifact.py` | `Artifact` ORM |
| `src/backend/core/llm/hooks.py` | 附件上下文注入（`_build_file_context` 等） |
| `src/backend/core/llm/agent_factory.py` | 项目 section 注入 system prompt |
| `src/backend/core/llm/tools/myspace_vfs.py` | 我的空间 ↔ 沙箱映射层 |
| `src/frontend/src/components/projects/` | 项目前端组件 |
| `src/frontend/src/components/myspace/` | 我的空间前端组件 |

相关文档：[记忆系统](./memory.md) · [对象存储](./storage.md) · [沙箱](./sandbox.md) · [知识库](./knowledge-base.md) · [认证与团队](./auth.md) · [版本对比](../editions/overview.md)
