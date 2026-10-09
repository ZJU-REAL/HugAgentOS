# 应用数据库与 MCP 托管

应用数据使用单独的 PostgreSQL，不复用平台数据库。静态站点不自动创建数据库；表单或持久化数据需求才创建应用与数据表；用户要求 MCP 时才发布 MCP。独立 MCP 应用不要求绑定站点。

## 本地和团队部署

配置 `APPLICATION_DATABASE_URL` 指向独立数据库。不能指向平台 `DATABASE_URL` 的同一数据库。云部署未配置时，管理界面提示未启用；本地模式可使用 SQLite，但生产使用 PostgreSQL。首次使用先执行 `python -m core.services.application_hosting_setup` 初始化注册表。

可选 Compose 覆盖文件 `docker-compose.application-hosting.yml` 包含独立 PostgreSQL、持久卷和一次性初始化任务。分别提供不同的 `APPLICATION_DB_PASSWORD`（初始化）与 `APPLICATION_OWNER_DB_PASSWORD`（运行账号），使用 URL 安全的密码或正确编码连接串；初始化任务使用与后端相同的 Dockerfile 构建目标，并只读挂载当前后端源码。部署时后端也必须使用本次源码构建的镜像，并按仓库部署流程重建前端，使 nginx 的 `/applications-mcp/` 转发配置生效。

```bash
docker compose -f docker-compose.yml -f docker-compose.application-hosting.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.application-hosting.yml up -d
```

实际启动、重建、迁移和部署仍遵守项目授权流程。存量迁移使用独立应用数据库。本地验证不代表远程环境已经升级。端到端验证使用独立数据库及平台的本地 mock 认证；该认证模式仅供本地测试。

## 用户入口与访问权限

站点管理弹窗的“数据与 MCP”展示关联数据库、表结构和记录，提供 JSON 导入、分页查看及最多 100 行 CSV 导出。站点页面使用同一套卡片展示站点、已定义工具的 MCP 服务和未关联有效站点的应用数据库。统一搜索支持站点标题、地址，以及 MCP 标题、工具名称和说明。关联站点的纯数据库仍在站点管理中查看。
MCP 卡片提供打开、复制链接、编辑和管理。“打开”展示连接地址、状态与工具清单，不直接访问协议端点；“编辑”进入该 MCP 的源码项目对话；“管理”继续提供工具配置表单，可添加或删除工具，修改名称、说明、数据表、返回字段和过滤字段。管理弹窗固定当前应用，提供数据与 MCP 两个标签页。发布更新后显示本次访问凭据；撤销访问需确认。
删除网页不自动删除应用数据或撤销 MCP，保留的数据库或 MCP 卡片仍可用于查看数据与撤销访问。REST 数据更新接口支持版本检查；当前界面没有单行编辑器。

前后端路径：浏览器管理接口 `/api/v1/applications`；后端路由 `/v1/applications`；公开表单提交 `/site/{slug}/__api/data/{table}`；MCP 地址 `/applications-mcp/{application_id}`。平台登录控制管理接口；站点的可见性与密码规则同时控制公开表单访问。只有显式设置 `public_insert` 的表允许匿名写入，匿名访客默认不能查询记录。共享数据表可显式开启 public_read；整体替换还需 public_replace，并提交读取时的 revision。过期 revision 返回 409。这里“匿名”是未登录访问者，并不表示收集的信息已经匿名化。

MCP 使用 Streamable HTTP 和独立 Bearer 凭据。发布时凭据只返回一次，数据库仅存哈希；重新发布、撤销和回滚配置会使旧凭据失效。客户端需发送 `Authorization: Bearer <凭据>`。MCP 查询与 REST 共用数据服务，发布仅暴露声明的工具、字段和过滤条件。配置与数据持久化，不依赖聊天或沙箱寿命。

PostgreSQL 每个应用使用独立 schema 与受限 NOLOGIN 角色；查询和写入会切换到该角色，不能访问其他应用或注册表。注册与建表仍依赖可信的数据库管理连接；不接受用户任意 SQL，也不向网页暴露数据库凭据。

## 产业知识中心

企业导入使用已有企业 MCP 的真实来源配置，要求企业版及已安装插件授权。通过 `manage_application` 的 `import_industry` 动作导入企业标识、名称、地址、状态和来源等字段，再发布声明式查询工具。来源无效或不可用时返回受控错误，不生成假企业作为成功结果。此版本使用导入快照，不保证源数据实时更新。

## 旧版存储退役

旧 KV、旧表单收集器及其管理接口、工具、前端入口已经移除，不再保留双轨业务逻辑。旧站点的 `__api/kv`、`__api/forms` 请求返回 404 或 405；升级前需将需要保留的页面改为 SQL 数据接口。

平台迁移 `site05retire` 在删除 `site_kv` 和 `site_submissions` 前，完整归档原记录，并校验 SHA-256。非空旧表还需要匹配的已验证 SQL 迁移回执；缺少回执会阻止删表。默认保存到 `STORAGE_PATH/migration-backups`，也可设置 `SITE_STORAGE_BACKUP_DIR`。归档文件权限为 0600，包含原业务信息，应纳入受控备份。归档失败会阻止删表。

需要恢复时，设置 `SITE_STORAGE_RESTORE_FILE` 为已核验的归档绝对路径，再按部署流程回退迁移。恢复还原原字段、时间戳、JSONB 与外键；恢复数据表不会自动恢复已删除的接口，需要同时恢复对应旧代码版本。历史 Alembic 迁移保留，用于已有安装的升级链。

## 维护结构

表结构与校验、关系表构建、事务与注册表、数据读写、MCP 部署、协议适配各自独立。REST、代理工具和 MCP 共用数据服务，避免权限与数据校验出现多套实现。前端将 API 与数据加载移出面板，切换应用时取消旧读取，并核对异步发布回执所属应用。

## 当前范围与限制

已实现结构化 SQL 表、约束与索引、原子批量写入、幂等重试、版本更新、受限查询、应用隔离、匿名表单和声明式 MCP 托管。请求有行大小、批量大小、分页和记录配额限制。CSV 流式导出全表。

未实现任意用户代码 MCP 容器、OAuth 授权服务器、外键关系设计器、全量备份恢复控制台和完整用量计费。当前固定 Bearer 方案不可宣称已完成 OAuth。真实聊天端到端驱动位于 `src/backend/tests/application_hosting_e2e.py`，独立测试环境由同目录的 `application_hosting_e2e_setup.py` 准备。通过生产聊天 API 验证模型、工具、沙箱与发布，验收结果以实际运行记录为准。

## 本地端到端复现

使用已配置真实模型、产业来源和 OpenSandbox 的本地 Docker 环境；先构建当前前端。以下驱动使用生产 API 与真实工具，不替换文件工具或发布接口。测试服务仅绑定回环端口，使用独立 platform_e2e_verified / applications_e2e_verified 数据库和独立存储目录。

```bash
npm --prefix src/frontend run build
PYTHONPATH=src/backend .venv/bin/python src/backend/tests/application_hosting_e2e_setup.py
PYTHONPATH=src/backend .venv/bin/python src/backend/tests/application_hosting_e2e.py
```

在 `http://127.0.0.1:18533/site/e2e-form-1007/` 用测试信息提交一次，邮箱使用 `browser-e2e@example.test`。再运行：

```bash
PYTHONPATH=src/backend .venv/bin/python src/backend/tests/application_hosting_lifecycle_e2e.py
```

生命周期验证会重启上述独立测试服务，并轮换、回滚、撤销和重新发布测试 MCP 凭据。凭据只保存在 `/tmp/application-hosting-full-e2e-credentials.json`（0600），不放入网页或验收报告。已有批次不自动清库；完整聊天重跑应使用干净的独立数据库批次。该流程不升级主环境，也不执行远程部署。

重跑完整聊天时，先停止并移除 `hugagent-application-e2e-backend` 和 `hugagent-application-e2e-mcp` 这两个测试容器，再以 `APPLICATION_E2E_BATCH=run2` 执行 setup。旧批次数据库保留，驱动仍使用相同的回环访问地址。不要删除主环境容器或数据库。

## 桌面端

纯本机模式在启动时初始化独立 applications.sqlite；静态页面不创建应用数据。
MCP 地址取配置的本机后端地址，仅本机可达。本机服务停止后，MCP 同时停止。
双端模式将正式站点、表单数据和 MCP 一并托管在云端。本机任务通过当前账号授权的
Sites 网关转发 manage_application，不回退本机数据库。代理忽略旧 local 标记。
界面和工具回执均使用固定的后端地址，不使用桌面窗口的随机端口。

桌面 SQLite 启动不执行完整 Alembic 链，因此在表结构调和前调用共享的旧存储归档逻辑；
未完成 SQL 迁移验证时，桌面保留旧表并继续启动；旧网页接口仍需转换。验证回执和备份均通过后才删除旧表。更新需重新交付桌面包，
双端还需更新云端后端与 Sites MCP，再同步能力。

桌面升级备份包含 storage/applications.sqlite 及其 WAL/SHM。升级失败时恢复原文件集，删除新版本产生的伴随文件。

SQL 的 json 列保存对象或数组，不支持 unique/indexed 标志或过滤条件。页面读取与整体替换均遵守站点可见性和密码规则。

存量迁移工具 application_site_migration 默认只做预检；MIGRATION_APPLY=1 才执行迁移。当前转换器仅覆盖已经盘点的三个模板。未知页面或键会停止迁移，须增加经过验证的转换器，不能直接删表。application_site_verification 核对原字段、权限、页面与源码备份后生成验证回执。


## 对话发布到个人 MCP

站点插件 1.7.0 新增 `mcp-builder` 技能和独立 `publish_mcp` 工具。对话示例：
“基于产业知识中心创建企业查询 MCP，并发布到我的个人 MCP”。
先用 `manage_application` 创建数据库、定义字段及导入真实记录，再调用
`publish_mcp(app_id, tools)`。服务端验证连接后创建仅当前用户可见的个人 MCP，
凭据加密保存在平台数据库，工具结果不包含凭据。需要管理员开放 `can_add_mcp`。
只有 `installed=true` 且 `connection_verified=true` 才表示个人接入成功。

重复发布复用同一条个人 MCP。通过站点管理更新或回滚时同步轮换个人连接凭据；
撤销发布同步禁用个人连接并清除凭据。发布与平台注册属于两个数据库事务，
注册或探测失败会返回部分成功状态；服务已发布，但个人接入未完成，使用原 app_id 重试修复。
并发发布和撤销按应用串行执行。手动对外客户端接入仍在站点管理获取访问凭据。
本地 Docker 注册使用后端内部地址，不能直接作为外部客户端地址；工具回执使用公开服务路径。
桌面混合模式使用已登录云端账号转发，不接受任意用户提供的连接地址，也不放开通用 MCP 的内网限制。
新的个人工具在下一轮对话加载，不保证本轮工具列表实时增加。

## MCP 项目编辑

创建 MCP 使用 `manage_application(action="create", payload={"title":"服务名称","kind":"mcp"})`。
后台生成个人项目与根文件 `mcp.json`。普通数据应用默认 kind=data，不自动生成项目；旧 MCP
首次发布或点击编辑时补建工程。重复编辑复用项目和已有项目会话；已删除项目返回明确错误。

`mcp.json` 包含 app_id、version 和 tools。修改文件不会立即改变线上服务。保存后调用
`manage_application(action="publish_project", app_id="<原编号>")`，或 owner API
`POST /v1/applications/{app_id}/mcp/project`。发布更新同一服务地址与个人连接，并保留版本记录。
app_id 不匹配、基准版本过期或工具定义无效时拒绝发布。通过 source 动作查看草稿与
published_definition（线上配置），核对后再更新基准版本，不能直接绕过冲突。

`POST /v1/applications/{app_id}/editor` 返回项目、源文件、当前草稿与发布配置，不返回秘密。
项目文件使用既有源码读写接口的 revision 校验。发布成功但同期文件发生变化时，回执
project_synced=false；保留新草稿并报告同步失败，核对线上版本后继续。桌面双端的编辑对话
与项目均走云端，保持与 MCP 托管和账号一致；不会将云项目挂到本机执行。

升级已有应用数据库时，在切换运行代码前重新执行 `python -m core.services.application_hosting_setup`。
它幂等新增 hosted_application_sources 注册表，不修改已有业务表、记录或凭据。无需平台 Alembic
迁移。回退代码时保留该表与项目文件；再次升级可继续使用原绑定，不删除应用数据。

管理表单和回滚发布遇到尚未发布的项目草稿时返回冲突，不覆盖草稿。项目发布会等待文件工具登记，发布后刷新挂载文件的新版本；只有这些同步步骤完成才报告 project_synced=true。

## 外部检验修复与升级

应用数据仍为可选模块。云端部署必须配置不同的 APPLICATION_DB_PASSWORD 与 APPLICATION_OWNER_DB_PASSWORD，并同时使用 docker-compose.yml 与 docker-compose.application-hosting.yml。首次部署及已有卷升级都执行 application-database-init；该步骤创建恢复历史表，把专用库对象交给 application_owner，并为已有应用角色补齐授权。后端仅使用 NOSUPERUSER、NOCREATEDB、CREATEROLE 的 application_owner；application_admin 只用于专用数据库初始化。CREATEROLE 是创建和回收应用隔离角色所需权限，因此必须使用独立 PostgreSQL 实例，不能复用业务平台数据库。初始化失败时不启动后端，不手工跳过依赖检查。

访客 API 的成功及错误响应均带 CORS。云端访客写入与密码尝试采用 Redis 原子共享计数；所有后端进程必须使用同一 Redis。Redis 未配置或不可用时返回 503。仅未配置 Redis 的本机单进程模式使用内存计数。

public_replace 默认为关闭。开启意味着匿名访客可替换或清空整张表；版本检查和限流不能阻止首次清空。每次替换在同一事务中保存原版本，最多保留20个快照（首个非空数据基线受保护，另保留最近19个版本；匿名替换不能淘汰基线），每份最多512 KiB；超过恢复上限时拒绝替换。管理员可在管理页面恢复历史版本，恢复须携带当前 revision，过期返回409，恢复后的记录生成新编号。

管理员可删除单行、数据表或应用。单行 DELETE 须携带 version；删除会清除该表恢复历史及应用幂等回执，防止被删除的数据通过历史恢复。数据表仍被 MCP 工具引用时返回409，需先删除工具定义。应用删除立即撤销 MCP 凭据并清除个人连接，然后删除专用 schema、角色和注册记录，释放应用配额。项目源码文件保留，需在项目管理中单独处理；数据库备份也须按运维保留策略管理。

CSV 按事务快照流式导出全表，JSON 字段使用标准 JSON 文本，保留表格公式防护。MCP GET、HEAD、DELETE 返回405，仅 POST 承载无状态协议请求，避免独立 SSE 长连接；limit 的工具参数明确限制为1至100。
