---
name: yida
description: >
  宜搭（钉钉低代码平台）AI 应用开发与管理总入口技能。通过沙箱内 openyida CLI 操作宜搭：
  创建应用/表单/流程/自定义页面、发布页面、增删改查表单数据、配置公式与业务规则、生成报表/图表/数据大屏、管理应用与表单权限等。
  当用户提到"宜搭"、"yida"、"低代码"、"创建应用"、"创建表单"、"发布页面"、"搭建"、"系统"等关键词时，使用此技能；以下情况不要触发：只是讨论通用前端/后端代码、非宜搭平台产品、或只需要解释概念而不操作宜搭资源。
cli_version: ">=2026.7.12"
allowed_tools: bash, sandbox_get_artifact, sandbox_put_artifact
---

# 宜搭 AI 应用开发指南

通过宜搭低代码平台实现一句话生成完整应用。所有操作通过沙箱内 **`openyida`** CLI 统一执行（已随沙箱镜像预装，**禁止**自行 npm install / 升级）。

## 运行环境与登录（本宿主必读，优先级最高）

1. **固定工作目录**：所有 `openyida` 命令**必须**在 `/home/ubuntu/yida-workspace` 目录下执行（每条 bash 命令以 `cd /home/ubuntu/yida-workspace && ` 开头）。该目录是持久卷：登录 cookie（`.cache/cookies-*.json`）、环境配置与项目产物（`prd/`、schema cache）都落在这里、跨会话存活。**在其它目录执行 openyida 会导致登录态丢失、产物散落，严禁。**本文其余部分提到的「项目根目录 / projectRoot / workspace 根」一律指该目录。
2. **登录流程（对话内扫码）**：用户也可在「插件 → 宜搭低代码平台」详情页扫码连接（与本流程写同一份登录态）；命令报未登录 / 401 / Cookie 失效时，先建议用户去插件详情页扫码，或直接在对话内走以下流程：
   - 执行 `cd /home/ubuntu/yida-workspace && openyida login --agent-qr`，从返回 JSON 中取 `qr_image_markdown`，**原样输出给用户**并请其用钉钉 App 扫码确认；
   - 用户答复已扫码后，执行返回 JSON 中的 `poll_command` 等待登录完成；
   - 若返回 `need_corp_selection`，把组织列表转述给用户选择，然后按子技能 `yida-login` 的降级路径用 `--corp-id <corpId>` 完成组织选择；
   - **不要**使用 `openyida login --browser`（沙箱无桌面浏览器），**不要**手动编造或写入 Cookie。
3. 登录成功后先跑任意只读命令（如 `openyida env --json`）验证 Cookie 可用，再做真实资源操作。

---

## 宿主能力适配

- 本宿主**没有** `use_skill` / `search_skills`。下文所有「加载子技能 `<技能名>`」一律理解为：**读取本技能目录下 `references/subskills/<技能名>/README.md`**（用文件读取工具）。
- 每个阶段只读取当前唯一需要的一个子技能文档；禁止并发批量读取多个子技能、禁止预读未来阶段技能。
- `references/` 下的其它辅助文件只能在已读取对应子技能后按需读取。
- 文中若提到 `skills-index.json` 或 OpenYida MCP 工具（如 `select_yida_login_organization`），本宿主不支持，忽略并走文档给出的降级路径。

---

## 第一步：只读预检（先于真实资源操作）

> ⚡ **前置门槛**：确认 openyida 已安装、Node/npm 依赖达标、登录态就绪。**未通过只读验证前，禁止创建应用/页面/表单或发布等任何真实资源操作。**

**怎么做**：优先跑一次 `openyida agent-capabilities --json`。该命令一次返回 version、cwd、AI 工具环境、推荐工作目录、登录态摘要、commands、command count、sideEffects 和默认 `fast_build` 契约，避免反复 `which openyida`、`openyida --version`、`openyida --help`、`openyida env`、`login --check-only`。

若当前 OpenYida 版本还没有 `agent-capabilities`，退回跑 `openyida env --json` 和 `openyida login --check-only --json`。旧版本地 agent 不需要认识 `skills-index.json`，也不需要支持 `agent-capabilities` 才能继续执行。

| 检测结果 | 处理 |
|---------|------|
| 命令跑不了（`command not found`） | 异常：openyida 应已随沙箱镜像预装。**不要**自行 `npm install`，直接报告用户"沙箱镜像缺少 openyida，请联系管理员重建沙箱镜像" |
| Node/npm 版本不达标 | 同上，属镜像问题，报告用户，不要自行升级 |
| `login.status` 不是 `ok` 且 `login.can_auto_use` 不是 true | 未登录 → 按顶部「运行环境与登录」的 `--agent-qr` 扫码流程处理 |
| `active.projectRootExists` 为 false | 在 `/home/ubuntu/yida-workspace` 下跑 `openyida copy` 初始化 |

**👉 环境异常、登录失败等特殊分支 → [references/setup-and-env.md](references/setup-and-env.md)。正常 `agent-capabilities` 通过时不要默认读取该 reference。**

---

## 第二步：意图路由（先判断「全量搭建」还是「单一任务」）

> ⚡ **环境就绪后，先判断用户诉求属于哪一类，再走对应路线**：从零搭一个完整应用，还是对已有资源做单点改动。选错会导致多余步骤或回退；歧义时简短确认一次即可。

| 用户诉求信号 | 判定 | 走哪条路线 |
|------------|------|-----------|
| 创建/搭建/做一个 + 应用/系统/管理系统；或明确表达从零开始 | **全量搭建** | 加载子技能 `yida-app`，由它执行完整应用 workflow |
| 对已有应用/表单/页面的单点操作（加字段、查改数据、配公式、建报表、改权限、发布、美化…） | **单一 / 增量任务** | 到 [技能路由](#技能路由单一--增量任务) 选定 **1 个**，加载对应子技能执行，不回退流程 |

---

## 完整开发流程（全量搭建）

> 📌 仅当第二步判定为「全量搭建」时进入；单一/增量任务请跳「技能路由」。
> 加载子技能 `yida-app`，由它负责完整应用 workflow、阶段子技能加载、关键 ID 流转、PRD 与 schema cache 约束。
> 用户说“按默认方案 / 不要追问 / 直接创建 / 尽快搭建”时，`yida-app` 选择 `fast_build`：创建应用、必要表单、主页面、发布并输出链接。

**默认链路**：`fast_build` 必须只做 `创建应用 → 核心表单 → 主页面 → 编写主页面源码 → 发布 → 返回访问链接`。不要因为应用名里有“看板 / 系统 / 管理”就升级到 `deep_design` 或 `full_demo`。

**fast_build 默认加载边界**：只加载 `yida-app` 和当前阶段必需的子技能：`yida-create-app`、`yida-create-form-page`、`yida-create-page`、`yida-custom-page`、`yida-publish-page`。Code Canvas 尚未全量，只有用户明确要求、已有页面为 `YidaCodeCanvas`，或已确认当前组织/页面支持时才加载 `yida-canvas-custom-page`。不要默认加载 `yida-page-uiux`、`yida-data-source-connectors`、`yida-data-management`、`yida-nav-group`、`yida-dashboard`，也不要默认深读 `references/`。

**doneWhen**：`yida-app` 发布主页面成功并输出可访问 URL。到这里默认完成；不要发布后继续 TaskCreate、重复读技能或继续规划。

**optionalAfterDone**：导航整理、示例数据、公开访问、截图验证、深度视觉方向、数据源/连接器深度接入、报表/大屏，只在用户明确要求或 `yida-app` 模式为 `full_demo` / `deep_design` 时执行。

---

## 技能路由（单一 / 增量任务）

> 选定 **1 个**最匹配的项执行。表**按业务域分组**，每组内既可能是子技能也可能是 CLI：
> - 行名为 `yida-xxx` / `sls-log-workbench` / `large-file-write` 的是**子技能** → 读取 `references/subskills/<技能名>/README.md` 后按其执行；
> - 行名为 `openyida xxx` 并标 **`CLI`** 的**无子技能文档** → 识别到诉求直接执行命令、不要去找 README。
>
> 按分组 +「何时选择」内联区别对号入座即可。

> ⚠️ **同类易错先分清**：改字段结构→`create-form-page`｜只读 Schema→`get-schema`｜改数据记录→`data-management`｜详情页美化→`form-detail`；自定义页视觉方向/去AI味→`page-uiux`(定方向)｜token/组件实现→`custom-page`(design-system)；加导航先分清→平台左侧菜单分组/排序→`nav-group`｜页面隐藏应用导航后页面内自绘导航壳→`nav-shell`（必须隐藏原导航，并让导航项 URL 带 `isRenderNav=false` 等参数）；字段实时校验→`formula`｜提交后编排→`integration`｜跨表高级函数→`business-rule`；从零建流程→`create-process`｜改已有流程→`process-rule`；权限按层级：组织→`corp-manager`／应用→`app-permission`／表单→`form-permission`／页面分享→`page-config`；**自定义页面选路见下方专表**。

> 🧭 **自定义页面选路（兼容优先，按顺序命中即停）**：
> 1. **默认 → native** `yida-custom-page`：平台全量兼容的 `.oyd.jsx` 链路，适合完整应用 `fast_build` 和未确认 Canvas 能力的组织；
> 2. 仅当用户明确要求 Code Canvas / 代码画布，已有页面 Schema 是 `YidaCodeCanvas`，或已确认当前组织/页面支持 Canvas → `yida-canvas-custom-page`；
> 3. 已有普通 `.oyd.jsx` 要迁到 Canvas，且目标组织支持 Canvas → `yida-canvas-upgrade`。
>
> 依据：Code Canvas 组件在宜搭平台侧尚未全量。native `.oyd.jsx` 是默认兼容链路；Canvas 代码在宿主页真实 `window` 中 `new Function` 执行，但物料只透传 `code/runtimeCode/importedModules/pageType`，无 `this` 上下文、无 `dataSourceMap`，`this.utils.yida.*` 不可用。需要实例桥或未确认 Canvas 支持时留 native。

| 分组 | 加载目标 | 何时选择（关键区别已内联） |
|------|------|--------------------------|
| **应用与登录** | 加载子技能 `yida-app` | 从零搭建整个应用；默认 `fast_build`，发布主页面拿到 URL 即完成 |
| | 加载子技能 `yida-create-app` | 只需创建应用、拿 appType |
| | 加载子技能 `yida-login` | 手动触发登录（通常自动触发） |
| | 加载子技能 `yida-logout` | 切换账号或组织 |
| **页面与表单** | 加载子技能 `yida-create-page` | 创建空白自定义页面拿 formUuid，后续写 JSX |
| | 加载子技能 `yida-create-form-page` | 创建/更新表单、增删改**字段结构**（普通表单，无审批） |
| | 加载子技能 `yida-create-process` | 从零建**带审批**流程表单（表单还不存在，一步到位） |
| | 加载子技能 `yida-page-uiux` | 单点页面美化、用户明确要求视觉方向/去 AI 味，或 `yida-app` 进入 `deep_design` 时使用；`fast_build` 不默认加载 |
| | 加载子技能 `yida-custom-page` | **自定义页面默认兼容链路**：native `.oyd.jsx`，适合完整应用 `fast_build` 和未确认 Canvas 能力的组织 |
| | 加载子技能 `yida-canvas-custom-page` | Code Canvas 可选链路：用户明确要求代码画布、已有页面为 `YidaCodeCanvas`，或已确认当前组织/页面支持 Canvas 时使用 |
| | 加载子技能 `yida-canvas-upgrade` | 将已有普通 `.oyd.jsx` / `Jsx` 页面升级迁移到 Code Canvas / `YidaCodeCanvas` 链路；仅在目标组织支持 Canvas 时执行 |
| | 加载子技能 `yida-nav-shell` | 自定义页**隐藏应用导航**（`isRenderNav=false`，沉浸/门户/大屏/分享）后，页面内用 JSX 自绘侧边/顶部/浮动/标签导航壳；发布后要配置隐藏原导航，跨页导航项要拼完整 URL 并合并 `isRenderNav=false` / `corpid` / 业务参数（**区别** `yida-nav-group` 平台左侧菜单分组：那是真实导航树，本项是页面内自建导航） |
| | 加载子技能 `yida-publish-page` | JSX 写完后编译并发布 |
| | 加载子技能 `yida-openyida-publish-guard` | 发布已有自定义页面前检查线上设计器状态，避免本地旧源码覆盖用户在线改动 |
| | 加载子技能 `yida-table-form` | Excel 式表格批量录入提交 |
| | 加载子技能 `yida-ppt-slider` | 全屏幻灯片页面（分享/路演/培训/演示） |
| | `openyida aggregate-table` `CLI` | 聚合表 / 虚拟视图（virtualView）：`list` 列出 · `create-empty` 建空白（返回设计器 URL）· `preview` 预览不保存 · `publish` 发布配置 |
| **数据可视化** | 加载子技能 `yida-report` | 普通报表/统计，开箱即用（原生 16 组件） |
| | 加载子技能 `yida-chart` | 更美观/定制化/数据大屏（ECharts） |
| | 加载子技能 `yida-dashboard` | 完整看板 / 驾驶舱产品化交付 |
| **连接器** | 加载子技能 `yida-connector` | 创建/管理连接器、配鉴权 |
| | 加载子技能 `yida-connector-safe-actions` | 连接器已有，从 API 代码生成执行动作 |
| | 加载子技能 `yida-data-source-connectors` | 用户明确要求通过设计器数据源/连接器调用外部 API 时使用；完整应用 `fast_build` 不默认加载 |
| **数据与公式** | 加载子技能 `yida-data-management` | 增删改查**数据记录**，不动字段结构 |
| | 加载子技能 `yida-get-schema` | **只读**查 Schema / 字段 ID，不改结构 |
| | 加载子技能 `yida-formula` | 配在**字段属性**上的实时计算/默认值/校验 |
| | 加载子技能 `yida-formula-evaluate` | 公式语法与字段引用静态检查 |
| | 加载子技能 `yida-business-rule` | 提交后**跨表**高级函数 INSERT/UPDATE/DELETE |
| **流程与自动化** | 加载子技能 `yida-process-rule` | **改已有**流程节点/分支/字段权限（表单已存在） |
| | 加载子技能 `yida-integration` | 提交后**逻辑编排**（图形化自动化流，推荐） |
| | 加载子技能 `yida-agent-center` | 流程代理（在职/离职代理人） |
| | `openyida ai-form-setting` `CLI` | 流程表单 AI 审批提示：`models` 查模型 · `fields` 查可插入字段（TEXT/IMAGE/ATTACHMENT）· `get` 查配置 |
| **权限与访问** | 加载子技能 `yida-corp-manager` | **组织级**权限（平台/子管理员、通讯录，影响整个组织） |
| | 加载子技能 `yida-app-permission` | **单应用级**权限（应用管理员/开发成员） |
| | 加载子技能 `yida-form-permission` | **单表单级**权限（权限组/数据范围） |
| | 加载子技能 `yida-page-config` | **页面级**：公开访问 / 组织内分享 |
| **应用配置与平台** | 加载子技能 `yida-nav-group` | 应用**左侧菜单**分组/排序（真实导航树；页面内自绘导航壳见 `yida-nav-shell`） |
| | 加载子技能 `yida-form-detail` | 只注 **CSS** 美化详情页，不改字段 |
| | 加载子技能 `yida-density` | 列表/表格信息密度选择 |
| | 加载子技能 `yida-i18n` | 应用多语言 / 国际化 |
| | 加载子技能 `yida-basic-info` | 组织版本/容量/域名/额度查询 |
| | 加载子技能 `yida-corp-efficiency` | 企业效能 / 低代码学习成果 |
| **辅助工具** | 加载子技能 `yida-flash-note-to-prd` | 会议纪要/闪记转 PRD |
| | 加载子技能 `yida-export-conversation` | 导出当前对话为 Markdown |
| | 加载子技能 `yida-voc` | 整理故障/需求反馈材料 |
| | 加载子技能 `sls-log-workbench` | SLS 平台问题日志查询 |
| | 加载子技能 `yida-db-seq-fix` | PostgreSQL 主键冲突 / Sequence 修复 |
| | 加载子技能 `large-file-write` | 可靠写入 100+ 行大文件 |
| | `openyida ai` `CLI` | 调用宜搭 AI 通用能力：文生文（文本生成）/ 识图（图片识别） |
| | `openyida batch` `CLI` | 批量顺序执行多条 OpenYida 命令（读 tasks 文件，支持 `--json --quiet`） |

---

## 核心规则

### 致命规则（FATAL，违反即失败/报错）

1. **技能加载唯一入口**：执行任何子技能前，必须先读取 `references/subskills/<技能名>/README.md` 获取该子技能的命令与参数格式，不凭记忆猜参数格式、不跳过文档直接执行。
2. **corpId 一致性检查**：创建页面前对比 prd 与 `.cache/cookies.json` 的 corpId，不一致必须询问用户（重新登录 or 当前组织新建）。
3. **发布前本地校验**：native `.oyd.jsx` / `.jsx` 页面发布前跑 `openyida check-page` + `openyida compile`；Code Canvas `.canvas.jsx` 不跑这两个 native 检查，改由 `openyida publish` 的 Canvas 编译阶段或 `compileCanvasLocal` 快检校验；JSON 配置写盘后先解析校验，再调用平台命令。
4. **命令输入文件禁止 shell 写入**：当 OpenYida 命令需要 JSON/YAML/CSV/config/script 文件参数时，先使用当前 agent 运行时提供的结构化文件写入工具（如 create_file / Write / file edit tool）创建文件，再把路径传给命令；禁止用 shell heredoc、`cat`/`echo`/`printf`/`tee` 加输出重定向，或把命令 stdout 重定向成业务文件。

### 重要规则（IMPORTANT，影响质量/性能/可维护性）

1. **按阶段加载必要技能**：按意图选 1 个主技能；完整应用按阶段加载当下唯一需要的子技能，禁止并发批量读取多个 `SKILL.md` 或预读未来阶段技能。
2. **优先复用缓存**：`appType`/`formUuid`/`fieldId` 优先从 `.cache/<项目名>-schema.json` 读，缺失再 `get-schema`。
3. **模板优先**：复杂产物先用 `openyida sample` 或现有示例生成骨架，再做最小改动。
4. **配置承载优先于代码**：字段/公式/联动/报表/审批/集成交给对应技能，自定义页面只做展示与胶水。
5. **数据性能优先**：统计聚合用 `yida-report` 服务端聚合，不在前端拉全量后自行聚合。
6. **避免无效重试**：失败先查登录态/组织/参数/字段 ID，无修改不连续重试超 1 次。
7. **配置分两处存**：业务语义 → `prd/<项目名>.md`；Schema ID → `.cache/<项目名>-schema.json`（prd 不记 ID）。
8. **临时文件入 project `.cache/`**：OpenYida 业务中间文件写入 `<projectRoot>/.cache/openyida/<项目名或任务名>/`；Schema ID 映射仍写 `<projectRoot>/.cache/<项目名>-schema.json`。从 workspace 根执行命令时使用 `project/.cache/...`，从 project 工作目录内执行时使用 `.cache/...`；不要写仓库根目录或系统临时目录。
9. **报表美化先问方案**：用户说"优化/美化报表"时先问选原生报表(`yida-report`)还是 ECharts(`yida-chart`)。
10. **按 schema 证据选技能**：先看 `formType`、组件树、`dataSource.online`；`receipt/process/report` 分别落到表单/流程/报表技能。
11. **官方示例范式优先**：蒸馏官方示例时先理解脱敏 schema 承载方式，不凭截图/标题/视觉判断。
12. **默认完成即停止**：完整应用默认以发布成功并输出 URL 为 doneWhen；UIUX、数据源深读、示例数据、导航、截图、TaskCreate 和深度设计都是 optionalAfterDone。
13. **数据查询与超长结果纪律（本宿主强约束）**：`data query form` 必须小页拉取（`--size` ≤ 10）并用 `--search-json` 按人/时间等条件精确过滤，拿到目标记录立即停止，禁止整表翻页核对。工具结果超长会被落盘到 `/workspace/.offload/`、正文只留预览——**禁止用 Read 整读 offload 文件**（内容多为单行大 JSON，整读会把几十万字符灌回上下文，触发反复压缩、把整轮对话拖到分钟级）；需要提取时用 Grep 带精确 pattern，或 bash `jq` / `python -c` / `head -c 2000` 只取所需字段。同一查询失败或结果不符时先改条件，不要原样重试。

> 📖 每条规则的完整说明、PRD 质量门槛、临时文件路径规范、报表美化话术 → [references/development-rules.md](references/development-rules.md)

---

## 常见问题

| 问题 | 处理 |
|------|------|
| `app-list` 返回「暂无应用」/ 看不到组织级应用 | **`app-list` 只列当前用户「本人创建」的应用**（接口按 creator 过滤），组织里他人创建的应用天然不在列表里，这不代表无权限。请用户提供应用访问链接（含 `APP_XXXX` 的 appType），用 `openyida list-forms <appType> --json` 确认可达后直接以 appType 操作（get-schema / 数据 / 报表等命令均不依赖 app-list 可见性）；被服务端拒绝才是真无权限，引导用户找管理员加开发成员（`yida-app-permission`） |
| 发布提示登录失效 | 先 `openyida login`，再 `openyida publish <源文件> <appType> <formUuid> --health-check` |
| 查已有表单的字段 ID | `openyida get-schema <appType> <formUuid>`，从 Schema 读各字段 `fieldId`（详见 `yida-get-schema`） |
| 更新已有表单字段 | 用 `create-form` 的 update 模式：`openyida create-form update <appType> <formUuid> '[{"action":"add","field":{"type":"TextField","label":"新字段"}}]'`（详见 `yida-create-form-page`） |
| 发布提示 corpId 不匹配 | 问用户：当前组织新建应用发布，或 `openyida logout` 后重新登录到正确组织 |

---

## 参考文件

| 文档 | 覆盖范围 | 何时阅读 |
|------|---------|---------|
| [环境准备与登录检测](references/setup-and-env.md) | 环境依赖、env 解读、多环境登录、悟空降级、Codex handoff、project 初始化 | 环境异常或登录问题时 |
| [核心规则详解](references/development-rules.md) | 成功率清单、PRD 门槛、临时文件、报表美化、corpId | 编写 PRD / 规范执行前 |
| [字段类型 / URL 规则](references/field-and-url-reference.md) | 表单字段类型速查、应用 URL 拼接规则 | 建表单 / 拼访问链接时 |
| [宜搭 API](references/yida-api.md) | 宜搭 API 完整参数 | 调用 API 前 |
| [公式函数库](references/formula-functions.md) | 公式函数速查 | 编写公式前 |
| [官方示例 Schema 范式](references/official-example-schema-patterns.md) | 脱敏 schema 承载范式 | 蒸馏官方示例时 |
| [查询条件构造](references/query-condition-guide.md) | 数据查询条件写法 | 数据查询/筛选时 |
| [报表字段配置](references/report-field-config-guide.md) | 报表字段配置规范 | 配置报表时 |
| [版本功能差异](references/edition-features-guide.md) | 各版本能力差异 | 版本能力查询时 |
| [模型 API](references/model-api.md) | AI 模型接口 | 调用宜搭 AI 模型时 |
