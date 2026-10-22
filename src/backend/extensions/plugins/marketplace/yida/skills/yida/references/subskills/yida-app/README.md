
# yida-app — 完整应用编排契约

本技能只做完整应用流程编排，不承担全局单点任务路由。进入每个阶段前，按根入口的宿主能力适配规则只加载当前阶段唯一需要的子技能；不要预读未来阶段，不要批量加载技能。

## 触发条件

用户要求从零创建、搭建、生成一个完整宜搭应用/系统/平台/管理工具时使用本技能。

用户说“按默认方案”“不要追问”“直接创建”“尽快搭建”等，必须选择 `fast_build`，用合理 MVP 假设直接执行，不展开深度 PRD 讨论。

**默认判定**：从零搭建完整应用时，只要用户表达“默认方案 / 不要追问 / 直接创建 / 尽快搭建”等快速交付信号，就必须命中 `fast_build`。默认完成链路固定为 `创建应用 → 核心表单 → 主页面 → 编写主页面源码 → 发布 → 返回访问链接`。

## 模式

| 模式 | 何时使用 | 目标 |
|------|----------|------|
| `fast_build` | 默认；用户要求不追问/直接创建 | 创建应用、必要表单、主页面，发布并输出访问 URL |
| `full_demo` | 用户明确要演示完整、示例数据、导航整理、可点验收 | 在 `fast_build` 后补导航、示例数据、公开访问或截图 |
| `deep_design` | 用户明确要深度产品设计、视觉方向、多角色、多页面、复杂流程 | 先做详细 PRD/视觉决策，再执行多阶段搭建 |

默认不要把 `full_demo` / `deep_design` 的动作塞进 `fast_build`。不要因为用户说“看板”“系统”“管理”就自动加载视觉决策、数据源、示例数据、导航整理或截图验收。

## 预检

遵循根入口的只读预检结果。若当前会话还没做预检，先按根入口执行一次只读校验；只有登录态可用后，才执行会创建、修改或发布宜搭资源的命令。不要在每个阶段重复跑 env/help/login 探测。

## fast_build 阶段

| 阶段 | 子技能 | 必做动作 | doneWhen |
|------|--------|----------|----------|
| 1. 创建应用 | `yida-create-app` | `openyida create-app`，提取 `appType` | 拿到真实 `appType` |
| 2. 记录最小需求 | 无 | 写 `prd/<项目名>.md`：只记录 MVP 假设、核心表单/页面、完成标准；写 `.cache/<项目名>-schema.json` 初始骨架；不要写长 PRD | 业务语义和 ID 存储位置明确 |
| 3. 创建必要表单 | `yida-create-form-page` | 只创建支撑 MVP 的核心表单；字段配置文件写入 `.cache/openyida/<项目名>/` | 拿到表单 `formUuid` 和真实 `fieldId` |
| 4. 创建主页面 | `yida-create-page` | 创建一个用户主入口页面，通常是工作台/看板/列表入口 | 拿到页面 `formUuid` |
| 5. 编写页面 | 默认 `yida-custom-page`；用户明确要求或已确认支持 Canvas 时才用 `yida-canvas-custom-page` | 生成主页面源码；只实现 MVP 首屏和核心操作。可用已创建表单链接、轻量统计占位和基础列表/入口布局完成主页面；不要加载视觉/密度/报表/数据源等额外技能 | 本地源码通过对应页面技能的基础校验 |
| 6. 发布页面 | `yida-publish-page` | 按页面链路校验后发布：默认 native `.oyd.jsx` / `.jsx` 跑 `check-page` / `compile` 后发布；Canvas `.canvas.jsx` 仅在已选择 Canvas 链路时用 `openyida publish` 的 Canvas 编译阶段或 `compileCanvasLocal` 快检；再发布主页面 | 发布成功并获得可访问 URL |
| 7. 输出结果 | 无 | 返回应用链接、主页面链接、已创建资源摘要、后续可选项 | 用户拿到 URL |

`fast_build` 不默认执行：`yida-page-uiux`、`yida-canvas-custom-page`（除非已确认 Canvas 支持或用户明确要求）、`yida-data-source-connectors`、`yida-data-management`、`yida-nav-group`、`yida-dashboard`、导航重排、示例数据、截图验收、公开访问配置、深度 UI 设计、长 PRD、TaskCreate / 继续规划任务，也不默认读取 `references/app-build-contract.md`。

## full_demo 可选后置

仅当用户要求，或模式明确为 `full_demo` / `deep_design` 时执行：

| 可选项 | 子技能 | doneWhen |
|--------|--------|----------|
| 导航整理 | `yida-nav-group` | 主页面/核心表单顺序符合业务入口 |
| 示例数据 | `yida-data-management` | 写入少量示例记录并 query 抽查成功 |
| 报表/图表 | `yida-report` 或 `yida-chart` | 报表或图表页面已创建/发布 |
| 公开访问 | `yida-page-config` | 分享配置保存成功 |
| 截图/人工验收 | 按宿主能力 | 截图或用户确认通过 |

## deep_design 附加要求

进入 `deep_design` 时，可以读取 [详细编排参考](references/app-build-contract.md)，并按需加载 `yida-page-uiux`、`yida-density`、`yida-dashboard`、`yida-data-source-connectors` 等设计/看板/数据源技能。不要在 `fast_build` 中默认读取这些参考或技能。

## 完成条件

完整应用的默认完成条件：

1. 主页面发布成功；
2. 输出可访问 URL；
3. 输出真实 `appType`、页面 `formUuid`、核心表单 `formUuid` 摘要；
4. 未继续执行可选后置动作。

发布成功并拿到访问 URL 后即完成，不要继续 TaskCreate、重复读技能、重复规划后续阶段。

## 错误处理

- 不编造 `appType`、`formUuid`、`fieldId`、`reportId`。
- 同一命令失败后，必须改变登录态、组织、参数、输入文件或字段 ID 后才能重试；禁止无修改连续重试。
- corpId 与目标组织不一致时先停下，让用户选择重新登录或在当前组织继续。
- 输入 JSON/YAML/CSV/JSX 等业务文件必须用结构化文件写入工具创建，不用 shell heredoc、`cat`、`echo`、`printf`、`tee` 或重定向。

## 存储约定

- 业务语义：`prd/<项目名>.md`
- 真实 ID：`.cache/<项目名>-schema.json`
- 临时配置/导入数据/脚本：`.cache/openyida/<项目名或任务名>/`
- 从 workspace 根执行命令时路径加 `project/` 前缀；在 OpenYida project 工作目录内执行时使用 `.cache/...`

## 参考

- [详细编排参考](references/app-build-contract.md)：仅 `full_demo` / `deep_design` / 排障时读取；包含 PRD 模板、字段文件示例、URL 规则、典型场景、删除应用确认、故障处理。
