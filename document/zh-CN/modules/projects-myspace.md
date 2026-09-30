# 项目空间与我的空间

> 最后更新：2026-07-31

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

### 项目侧栏交互

左侧栏会把每个项目及其所属对话显示为一个分组，项目行提供以下快捷操作：

- 单击项目名称区域，展开或收起该项目的对话列表；
- 单击新建对话按钮，直接进入一个已绑定该项目的空白对话；
- 打开更多菜单，可将项目置顶、在项目面板中打开，或移除项目。置顶与移除仅对
  项目管理员可用；移除会软删除项目，已有对话会回落到普通历史对话列表。

### 桌面端新建项目入口

桌面端的新建入口严格跟随安装时选定的运行形态，项目列表页与聊天输入区的项目下拉保持一致：

- **纯本机（`local_only`）**：只显示“新建本地项目”，通过系统文件夹选择器把本机目录建立为项目；不显示、也不能打开云端项目创建弹窗；
- **纯云端（`cloud_only`）**：只显示“新建云端项目”，不提供本机文件夹项目入口；
- **双模式（`dual`）**：同时提供云端项目和本地项目，由用户在新建时选择；
- **普通 Web**：没有桌面文件夹选择器，因此保持云端个人/团队项目创建流程。

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

进入团队文件夹时，默认加载所有有权限访问的团队目录，并展示各团队的一级文件夹；子目录按需展开，不在总览中平铺。左侧不显示展开箭头，点击目录行展开，再次点击收起。团队目录使用与个人文件夹相同的彩色图标；选择目录后查看对应文件。打开右侧 Canvas 预览后，目录导航与文件区仍保持左右排列，空间不足时优先隐藏文件大小、来源和时间等次要信息。

用户侧路由 `src/backend/edition_ee/routes/team_files.py`，挂 `multi_tenancy` 能力位（EE 路由表）；管理台对应 `/v1/config/teams/*`（`edition_ee/routes/config_teams.py`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/my-teams` | 我所在的团队 |
| GET / POST | `/v1/teams/{team_id}/folders` | 团队文件夹列表 / 创建 |
| PATCH / DELETE | `/v1/teams/{team_id}/folders/{folder_id}` | 重命名 / 删除 |
| GET / POST | `/v1/teams/{team_id}/files[, /upload]` | 团队文件列表 / 上传 |
| DELETE / POST | `/v1/teams/{team_id}/files/{artifact_id}[, /move]` | 删除 / 移动 |
| POST | `/v1/artifacts/{artifact_id}/move-to-team` | 个人文件转团队文件 |
| GET / PUT | `/v1/teams/{team_id}/members/permissions`、`.../{user_id}/permission` | 成员文件权限查看 / 调整 |

权限模型：`TeamMember.role`（owner/admin/member）+ `file_permission`（viewer/editor，仅对 member 生效），鉴权封装在 EE 专属的 `edition_ee/auth/team_permissions.py`。团队文件在沙箱侧有独立共享缓存 `team_cache_dir(team_id)`，同团队成员复用一份镜像。

## 文件如何进入对话上下文

三条路径，互为补充：

1. **附件注入（hooks）**：前端请求的 `attachments[]` 只携带 `file_id`、`name`、`mime_type`，不携带文件正文。`core/llm/hooks.py` 按 `file_id` 延迟下载、解析并缓存 `parsed_text` 与摘要；首轮只注入文件元数据和最多 5,000 字符的有界预览，xlsx 额外提供总行列数、表头和少量数据行。模型需要预览外的正文时通过 `read_artifact` 按需读取；图片仍走多模态注入。所有读取都校验 `user_id` 归属，防止伪造 `file_id` 跨用户访问；
2. **项目文件清单**：项目对话的 system prompt 携带挂钩文件夹的文件列表（见上文），agent 按需用 `read_artifact` / 沙箱工具读取具体内容；
3. **沙箱虚拟文件系统**：开启代码执行时，`/myspace/...` 路径把我的空间映射进沙箱（懒加载 + 文件系统实时登记），团队项目映射团队文件夹——见 [沙箱](./sandbox.md)。

## 相关源码

| 路径 | 职责 |
|---|---|
| `src/backend/api/routes/v1/projects.py` | 项目 API |
| `src/backend/core/services/project_service.py` / `project_file_service.py` | 项目业务逻辑 |
| `src/backend/core/services/project_scope.py` | `ProjectScope`（沙箱路径作用域） |
| `src/backend/api/routes/v1/myspace_folders.py` | 个人文件夹 API |
| `src/backend/api/routes/v1/artifacts.py` | 资产列表 / 会话收藏 / 加入知识库 |
| `src/backend/edition_ee/routes/team_files.py` | 团队文件夹与文件 API（商业版 EE） |
| `src/backend/api/routes/v1/file_upload.py` | 文件上传（可指定文件夹） |
| `src/backend/core/db/models/project.py` | `Project` / `ProjectFavorite` ORM |
| `src/backend/core/db/models/identity.py` | `UserFolder` 等共享身份 ORM |
| `src/backend/edition_ee/db/models/identity.py` | `Team` / `TeamMember` / `TeamFolder` ORM（仅 EE） |
| `src/backend/core/db/models/artifact.py` | `Artifact` ORM |
| `src/backend/core/llm/hooks.py` | 附件上下文注入（`_build_file_context` 等） |
| `src/backend/core/llm/agent_factory.py` | 项目 section 注入 system prompt |
| `src/backend/core/llm/tools/myspace_vfs.py` | 我的空间 ↔ 沙箱映射层 |
| `src/frontend/src/components/projects/` | 项目前端组件 |
| `src/frontend/src/components/sidebar/Sidebar.tsx` | 侧栏项目分组、快捷新建与项目菜单 |
| `src/frontend/src/components/myspace/` | 我的空间前端组件 |

相关文档：[记忆系统](./memory.md) · [对象存储](./storage.md) · [沙箱](./sandbox.md) · [知识库](./knowledge-base.md) · [认证与团队](./auth.md) · [版本对比](../editions/overview.md)

## 项目指令与 AGENTS.md

项目根目录的 `AGENTS.md` 是项目指令的正文来源。本地项目读取绑定的真实文件夹；云端个人/团队项目读取挂钩文件夹根级同名文件。子目录里的 AGENTS.md 不会被提升为整个项目的指令。

- 在根目录新建、上传或修改文件后，项目详情与后续对话读取最新正文。指令卡片每 10 秒及窗口重新聚焦时刷新；正在生成的回复继续使用该轮开始时的指令。
- 在「项目指令」编辑器保存会回写同一个 AGENTS.md。编辑器携带 `instructions_revision`；期间文件已变化时返回 409，需重新读取并合并，防止覆盖别人的修改。
- 尚未采用文件的旧项目继续使用已有数据库指令。首次发现文件或保存指令后采用文件；此后删除文件会清空有效指令，不恢复旧数据库文本。空指令在云端以换行文件存储。
- 文件必须为 UTF-8（支持 BOM），最多 32 KiB。不支持根文件符号链接；非法编码、超限、多个同名根文件或存储不可用均明确报错，不静默截断。

选择有编辑权限的具体项目后，在普通项目对话中输入 `/` 可看到「初始化指令」，也可以直接发送 `/init` 或 `/初始化指令`。默认聊天、只读项目以及计划/批量/工作流/自主循环模式不提供入口；初始化前需移除所选技能、插件、连接器或子智能体。本轮初始化使用标准工具模式，完成后不改变聊天的后续模式选择。

初始化提示词位于 `src/backend/prompts/project_init.md`。模型会检查现有规则和代表性资料，按项目类型创建或增量完善规则，保留人工约定，再通过绑定当前项目的 `read_project_instructions` / `save_project_instructions` 保存与读回。重复初始化不会要求重置已有规则。工具仍遵守运行时文件权限与审批模式；版本冲突或审批拒绝时不会当作保存成功。

这是项目内的初始化能力，不会自动执行发现的安装、部署或发布命令。无需数据库迁移；CE 与 EE 共用同步和初始化逻辑，文件作用域由各版本的项目服务提供。

## 个人项目转为团队项目（EE）

个人项目所有者可在项目详情的更多菜单选择「转为团队项目」，选择自己担任所有者或管理员的团队。转换将项目绑定的整个可见文件夹子树移到团队空间；原个人文件夹不再显示，项目编号、文件编号、文件内容和关联站点保持不变。已有同名项目或文件夹、文件夹与其他项目嵌套、仍有任务运行时，会拒绝转换；重复提交同一目标不会复制项目。目前不提供直接转回个人项目的操作。

转换是目录归属的数据库事务，对象存储键保持稳定，避免复制中断造成内容丢失。接口为 `POST /v1/projects/{project_id}/transfer-to-team`，请求体 `{"team_id":"..."}`。旧会话的过期项目范围必须重新加载后才能继续操作。私人对话不会自动共享，个人记忆也不会复制到团队；团队项目使用团队记忆范围。

Config「团队管理」新增成员或调整成员权限时，可选「所有者」「管理员」「编辑」「只读」。普通成员内部仍使用 `member` 角色，配合 `file_permission=editor/viewer`；用户端显示「编辑」或「只读」。

| 权限 | 读项目与源码 | 改源码、目标、发布站点 | 改项目名、删除项目、管理成员与站点 |
|---|---|---|---|
| 只读 | 可以 | 不可以 | 不可以 |
| 编辑 | 可以 | 可以 | 不可以 |
| 所有者 / 管理员 | 可以 | 可以 | 可以 |

源码保存接口为 `GET/PUT /v1/projects/{id}/source`，以项目内相对路径定位文件；保存必须提交读取时的 revision，避免覆盖其他成员的更新。Agent 的 Read/Edit/Write 和 bash 每次检查当前权限，权限降低后已有工具实例也不能继续写入。团队 bash 在会话工作副本 `/workspace/projects/{id}` 中执行，修改通过版本检查回写团队源码；删除源码请使用项目文件管理，shell 删除不会自动删除共享文件。

### 团队沙箱依赖与自动同步（商业版 EE）

团队挂载工作区的自动同步与 bash 结束后的补充保存，都忽略任意层级中名为
`node_modules`、`.git`、`__pycache__`、`.venv`、`.vite` 的路径及其内容。
排除规则同样适用于符号链接，包括在后端容器中目标不可见的链接；不会删除沙箱里的依赖。
其他源码路径仍执行符号链接安全检查和版本冲突检查。这是内置排除规则，不会读取项目的
`.gitignore`。

已成功发布的站点文件由托管服务独立保存，不随沙箱回收而删除。后续编辑可使用已同步的源码、
依赖清单和锁文件恢复构建环境；尚未同步的修改不能视为已经持久保存。


### 大量项目文件的浏览

项目文件区使用固定高度的虚拟列表，只渲染当前可见的文件行；展开目录、滚动、预览和删除仍然可用。项目指令区提供更宽的阅读区域和可调整高度的大编辑框。文件加载失败时可单独重试，不影响项目详情和指令。


### 个人文件路径唯一性

同一用户、同一目录只能有一个未删除的同名文件，根目录也受数据库唯一索引保护。
不同用户或不同目录可以使用相同名称；同一路径不能同时是文件和文件夹。
上传、生成、复制、移动造成重名时返回 HTTP 409；请使用原文件 ID 的修改接口，
编辑保留原 ID。删除后可以重新创建该名称。沙盒修改已有路径也更新原记录。

列表读取和执行沙盒命令前尝试一次正常同步。同步未完成不阻止读取或执行，
允许暂时看到上一状态。失败的登记留到下次正常同步再处理，不进行持续后台重试；
缺失文件或失败下载也在下次正常同步时补齐。

升级前先停止本地写入并备份数据库及 myspace_cache。
运行 `python src/backend/scripts/clean_myspace_duplicates.py --all` 预览；
加 `--apply --backup-dir <新的备份目录>` 才执行。
清理删除每个重名组的全部有效记录及镜像路径，不保留“最新一条”。
采用删除标记避免历史记录回填复活，原对象和恢复日志保留在维护备份中。
然后执行 Alembic 升级（EE: personalname01；CE: ce_0018），恢复服务并运行对账与端到端测试。
迁移遇到未清理重名会终止，不会自行删除其他环境数据。
恢复须先停止写入、撤销唯一索引，再由备份恢复；不要直接向正常服务恢复重名记录。

### 空间文件工具

空间文件工具统一由 `core/llm/tools/space_tools.py` 注册，调用名为 `space_list_myspace_files`、`space_stage_myspace_file`、`space_create_folder`、`space_move`、`space_delete`；商业版另提供 `space_list_team_files`。通用 Read/Write/Edit/Glob/Grep 名称保持不变。
收藏会话工具已移除；历史会话仍可通过 `list_related_chats` 和 `read_chat` 访问。


### 挂载目录统一同步

个人空间监听实现位于 `core/space_sync/`，团队适配位于 `edition_ee/services/space_sync/`。监听回调只入内存队列，后台批量写入挂载目录之外的 SQLite 日志，再执行带权限、版本检查的数据库及对象存储更新。原生移动保留文件和文件夹 ID；空目录、删除、重启恢复及解析缓存失效使用同一同步流程。数据库提交也会刷新已注册的挂载目录，回滚不刷新。冲突保留本地内容并使同步屏障报告失败。网络下载、上传和人工审批时间不属于回调延迟，完整同步延迟须按实际环境测量。
