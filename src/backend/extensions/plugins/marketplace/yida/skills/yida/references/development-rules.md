# 核心规则详解

> 逐条展开主 SKILL.md「核心规则」的操作细节，编号与主文件一一对应（FATAL 1–4 / IMPORTANT 1–11）。需要更多篇幅的规则指向文末专节。

## 致命规则（FATAL）

**F1 技能加载唯一入口**：执行任何子技能前，支持 `use_skill` 的宿主必须调用 `use_skill("<技能名>", "<本阶段目的>")` 加载对应技能；不要用 `Read` / `read_file` / `cat` 读取 `SKILL.md` 路径。`skills-index.json` 仅供 yida-agent 或同构宿主机器发现，不支持该索引的宿主忽略它。完全没有 `use_skill` / `search_skills` 的本地工具，才允许按根技能路由表和技能包相对路径逐个读取当前阶段唯一必要的技能文档；禁止并发批量读取多个 `SKILL.md`，禁止预读未来阶段技能。

**F2 corpId 一致性检查**：创建页面前对比 prd 文档与 `.cache/cookies.json` 的 corpId——
- 一致 → 继续；
- 不一致 → 询问用户：重新登录到正确组织，还是在当前组织新建应用。

**F3 发布前本地校验**：
- 自定义页面发布前先跑 `openyida check-page <源文件>` + `openyida compile <源文件>`；
- 发布时留意"同名双副本内容不一致"警告，必要时加 `--health-check` 做首屏 HTTP 健康检查；
- 任何 JSON 配置写盘后先做 JSON 解析校验，再调用平台命令。

**F4 命令输入文件禁止 shell 写入**：当 OpenYida 命令需要 JSON/YAML/CSV/config/script 文件参数时，必须先使用当前 agent 运行时提供的结构化文件写入工具（如 create_file / Write / file edit tool）创建文件，再把文件路径传给命令。禁止用 shell heredoc、`cat`/`echo`/`printf`/`tee` 加输出重定向，或把 `openyida` 命令 stdout 重定向成业务配置、Schema、导入数据或一次性脚本。

## 重要规则（IMPORTANT）

| # | 规则 | 操作细节 |
|---|------|---------|
| 1 | 按阶段加载必要技能 | 按意图选定 1 个主技能；完整应用按阶段加载当下唯一需要的子技能，仅在已加载技能明确要求时才读对应 `references/`。 |
| 2 | 优先复用缓存 | `appType`/`formUuid`/`fieldId`/`reportId` 先从 `.cache/<项目名>-schema.json` 读，缺失或不确定再 `openyida get-schema`。 |
| 3 | 模板优先 | 自定义页面、表单字段、报表配置等复杂产物先用 `openyida sample` 或现有示例生成骨架，再做最小改动。 |
| 4 | 配置承载优先于代码 | 字段结构/公式/联动/报表聚合/审批/集成/连接器动作交给对应技能承载；自定义页面代码只做展示、事件分发和必要胶水。 |
| 5 | 数据性能优先 | 统计聚合用 `yida-report` 服务端聚合；不在自定义页面前端分页拉全量后自行聚合。 |
| 6 | 避免无效重试 | 失败先按错误信息查登录态/组织/参数/字段 ID；无修改不连续重试超 1 次。 |
| 7 | 配置分两处存 | 详见 [配置信息分两处存储](#配置信息分两处存储) 与 [PRD 质量门槛](#prd-质量门槛)。 |
| 8 | 临时文件入 project `.cache/` | 详见 [临时文件规范](#临时文件规范)。 |
| 9 | 报表美化先问方案 | 详见 [报表优化 / 美化提示规则](#报表优化--美化提示规则)。 |
| 10 | 按 schema 证据选技能 | 先看 `formType`、组件树、`dataSource.online`；`receipt/process/report` 分别落到表单/流程/报表技能，只有默认页是自定义展示页、或确需列表/看板/工具页交互时才落到 `yida-custom-page`。 |
| 11 | 官方示例范式优先 | 蒸馏宜搭示例中心时，先按 [官方示例 Schema 范式](official-example-schema-patterns.md) 理解脱敏 schema 的承载方式，不凭截图/卡片标题/页面视觉判断。 |

## 配置信息分两处存储

| 信息类型 | 存储位置 | 内容示例 |
|---------|---------|---------|
| 业务语义信息 | `prd/<项目名>.md` | 字段名称、字段类型、字段说明 |
| Schema ID | `.cache/<项目名>-schema.json` | `appType`、`formUuid`、`fieldId` |

> prd 文档**不记录** `formUuid`/`fieldId` 等 ID，这些只写入 `.cache/`。

## PRD 质量门槛

`prd/<项目名>.md` 不是需求摘要，必须能直接驱动后续技能执行，至少包含：

- **MVP 范围**：V1 必做 / V1 不做 / V2 候选
- **目标用户与权限**：角色、入口、可执行操作
- **核心用户旅程**：完成关键任务的步骤与成功标准
- **页面与表单配置**：业务语义字段（不写 `formUuid`/`fieldId`）
- **流程与状态机**：状态、允许操作、下一状态、操作角色
- **数据关联与约束**：唯一性、关联关系、冲突校验、并发/重复提交风险
- **交互与验收标准**：用可验证标准描述"好用"
- **落地约束**：Schema 写 `.cache/<项目名>-schema.json`，发布前 `check-page` + `compile`

## 临时文件规范

所有 OpenYida 业务中间文件（cookies、schema 缓存、字段/报表/流程配置、导入数据、一次性脚本等）**必须写在 OpenYida project 工作目录的 `.cache/` 下**，不写业务仓库根目录、系统 `/tmp` 或其他位置。源码中的 `<projectRoot>` 指 OpenYida project 工作目录；从 workspace 根执行命令时路径通常是 `project/.cache/...`，从 project 工作目录内执行时路径是 `.cache/...`。

文件必须由 agent 的结构化文件写入工具创建，再传给 OpenYida 命令。不要通过 `execute_shell` 加 heredoc、`cat`/`echo`/`printf`/`tee`、管道或重定向来生成 JSON/YAML/CSV/config/script 文件。`/tmp` 只允许用于外部工具强制要求的系统临时路径，OpenYida 业务配置、schema、导入数据和一次性脚本不写 `/tmp`。

| 工件类型 | 推荐位置 |
|---------|---------|
| Schema / ID 映射 | `<projectRoot>/.cache/<项目名>-schema.json` |
| 字段 / 报表 / 流程 / 连接器配置 | `<projectRoot>/.cache/openyida/<项目名或任务名>/` |
| 批量导入数据（JSON/JSONL/CSV） | `<projectRoot>/.cache/openyida/<项目名或任务名>/data-import/` |
| 一次性 Python / JS 脚本 | `<projectRoot>/.cache/openyida/<项目名或任务名>/scripts/` |

> 只有需长期维护的 PRD、页面源码、示例资源才写入 `prd/`、`pages/src/`、`project/` 等正式目录。

## 报表优化 / 美化提示规则

用户提到"优化 / 美化 / 更好看 / 不够漂亮"等报表视觉相关诉求时，**先问用户选哪种方案**，再动手：

| 方案 | 做法 | 适用 | 落到 |
|------|------|------|------|
| A 优化原生报表 | 调整图表类型、布局、筛选器，仍用宜搭原生组件 | 快速优化、无需写代码 | `yida-report`（`openyida create-report`） |
| B ECharts 高级报表 | ECharts + 自定义页面 JSX，高度定制 | 精美视觉、复杂交互、数据大屏 | 读 `yida-chart` 子技能文档 |

话术示例：

> 我可以两种方式帮你优化报表：**①优化原生报表**——调整图表类型组合、布局、筛选器，快速提升；**②ECharts 高级报表**——自定义页面实现渐变、动画、自定义主题等精美效果。你选哪种？
