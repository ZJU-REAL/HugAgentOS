# 前端架构详解

> 最后更新：2026-06-11

前端位于 `src/frontend/`，是一个 React 19 + TypeScript 单页应用：Vite 构建、Ant Design 组件库、Zustand 状态管理、无路由框架——按 URL 路径在入口处切换五个独立应用壳。生产环境构建产物由 Nginx 容器托管，所有后端调用收敛到一个类型化客户端 `api.ts`。

## 多入口应用壳

`src/main.tsx` 按路径与查询参数选择渲染哪一个应用（统一包裹 antd `ConfigProvider` 主题与中文 locale）：

| 入口 | 触发条件 | 职责 |
|---|---|---|
| `App.tsx` | 默认 | 主对话应用：侧边栏、聊天区、能力中心、我的空间、项目、实验室等 |
| `AdminApp.tsx`（商业版 EE） | 路径 `/admin` | 内容管理台：技能、提示词、知识库、智能体、MCP、计费等 |
| `ConfigApp.tsx`（商业版 EE） | 路径 `/config` | 系统管理台：用户、团队、注册码、安全审计、license |
| `ApiDocApp.tsx` | 路径 `/api-docs` | 对外 API 文档页 |
| `SharePreviewApp.tsx` | 查询参数 `?share` | 会话分享只读预览页 |

CE 派生树物理移除 `AdminApp.tsx` / `ConfigApp.tsx` 及 `components/admin/`、`components/config/`（由 `ce/manifest.yaml` 声明），并用 overlay 提供精简版 `main.tsx`。

## api.ts — 类型化 API 客户端

`src/api.ts`（约 2200 行）是唯一的后端出入口：

- **基础地址**：`getApiUrl()` 返回 `import.meta.env.VITE_API_BASE_URL || '/api'`——开发态走 Vite 代理，生产态走 Nginx 同源 `/api`；
- **信封解包**：后端统一返回 `{ code, message, data, trace_id, timestamp }`，`isApiEnvelope` + `unwrapData<T>` 自动取出 `data`，调用方拿到的就是业务类型；
- **认证联动**：`onUnauthorized(handler)` 注册 401 回调，由 `authStore` 统一跳转登录；
- **错误友好化**：如 `uploadErrorMessage` 识别 Nginx 413（超出 `client_max_body_size`）等非 JSON 错误并给出中文提示；
- **领域函数**：会话 CRUD / 流式与续播 / 消息反馈 / 知识库 / 产物 / 自动化 / 批量 / 项目 / 记忆 / 能力中心等数百个具名函数，与 `types.ts` 中的共享类型一一对应。

SSE 流式不走 `api.ts` 的 JSON 通道，由 `hooks/useStreaming.ts` 直接消费 `fetch` 流。

## 组件组（components/ 下 22 组）

| 组件组 | 职责 |
|---|---|
| `admin/`（商业版 EE） | 内容管理台面板：技能 / 知识库 / 智能体管理器、页面配置编辑器、图标选择器等 |
| `agent/` | 子智能体的创建页、表单、@提及弹层、面板 |
| `apidoc/` | API 文档页面板 |
| `automation/` | 自动化运行时间线面板 |
| `batch/` | 批量执行确认弹窗与进度面板 |
| `canvas/` | 数据画布：Univer 在线表格面板 |
| `catalog/` | 能力中心：技能 / MCP 页面、目录面板、技能市场弹窗、图标选择 |
| `chat/` | 聊天主区：消息气泡、输入区、产物卡片、思考面板、计划卡片、技能斜杠弹层、文件确认条等 |
| `citation/` | 引用角标与引用感知的 Markdown / HTML 渲染块 |
| `common/` | 通用件：加载骨架、登录过期弹窗、品牌 Loader、计时器、图片预览 |
| `config/`（商业版 EE） | 系统管理台面板：聊天历史审查、注册码、license、沙箱实例、安全审计日志等 |
| `docs/` | 应用中心与文档面板（版本说明等） |
| `file/` | 附件卡片、文件预览窗格、我的空间导入弹窗 |
| `kb/` | 知识库创建与重建索引弹窗 |
| `lab/` | 实验室：自动化卡片 / 创建 / 详情 / 面板 |
| `memory/` | 记忆事实列表 |
| `myspace/` | 我的空间：文档 / 收藏 / 图片 / 通知列表与主面板 |
| `projects/` | 项目工作空间：卡片、详情面板、右栏、记忆弹窗 |
| `settings/` | 设置弹窗、个人 API-Key 面板、团队区块 |
| `share/` | 分享记录页 |
| `sidebar/` | 侧边栏、全局搜索弹窗、导航项定义 |
| `tool/` | 工具调用时间线：调用行、思考行、输出渲染器、结果面板、进度内联 |

## Zustand 状态（stores/ 下 18 个）

| Store | 职责 |
|---|---|
| `chatStore` | 会话列表、当前会话、消息流（核心状态） |
| `authStore` | 登录态、当前用户、401 跳转与登录落地页 |
| `uiStore` | 全局 UI：面板开合、当前视图、弹窗状态 |
| `catalogStore` | 能力目录（技能 / 智能体 / MCP / KB）及启停 |
| `agentStore` | 子智能体列表与选中态 |
| `settingsStore` | 用户设置（记忆开关、模型偏好等） |
| `fileStore` | 上传附件与解析状态 |
| `kbStore` | 知识库空间 / 文档 / 分块状态 |
| `mySpaceStore` | 我的空间资源树与收藏 |
| `projectStore` | 项目列表、详情、项目内会话 |
| `batchStore` | 批量计划状态（按 plan_id 键控，含待确认弹窗追踪） |
| `automationStore` / `automationChatStore` | 自动化任务列表 / 自动化会话运行态 |
| `canvasStore` | 数据画布文档状态 |
| `skillDistillStore` | 个人技能蒸馏任务状态 |
| `pageConfigStore` | 页面配置（品牌、导航、文案——驱动 white-label） |
| `editionStore` | `/v1/meta/edition` 探针的消费端：版本与 license 能力位布尔表 |
| `modelCapabilitiesStore` | 主模型能力探测（思考 / 视觉等） |

## Hooks

| Hook | 职责 |
|---|---|
| `useStreaming` | SSE 主消费器：暴露 `send` / `abort` / `regenerate` / `editAndResend` / `resumeRunIfAny`，解析 `content/thinking/tool_call/tool_result/tool_progress/meta/error` 事件，维护文本分段与工具时间线，支持 run 续播 |
| `useChatActions` | 会话管理动作封装：新建 / 删除 / 重命名 / 置顶收藏 / 导出 / 分享 / 摘要与分类 |
| `useChatInit` | 应用启动时的会话初始化与活动 run 恢复 |
| `usePlanMode` | 计划模式 SSE 消费器（首次执行与续播共用） |
| `usePageConfig` | 按 dot-path 读取页面配置（如 `branding.product_name`） |
| `useStallDetector` | 流静默超时检测（卡顿提示） |
| `useDelayedFlag` | 防闪烁的延迟布尔量（延迟显示 + 最短保持） |

## 样式与其他目录

- `styles/`：按功能域拆分的全局 CSS（`chat.css`、`catalog.css`、`automation.css`、`canvas.css`、`myspace.css`、`projects.css`、`config.css`、`team-folder.css` 等 18 个），`variables.css` 收口设计变量，`index.ts` 统一引入；
- `types.ts` + `types/`：共享类型（`ChatMessage`、`CitationItem`、`Catalog`…），团队文件等领域类型在 `types/teamFiles.ts`；
- `utils/`：`citations.ts`（引用解析）、`markdown.ts`、`segments.ts`（流式分段）、`fileParser.ts`、`adminApi.ts`、`pageConfigDefaults.ts` 等 22 个工具模块；
- `storage.ts`：localStorage 持久化与默认目录；`appTheme.ts`：antd 主题令牌；`preloadReload.ts`：chunk 预加载失败自动刷新。

## 国际化（i18n）

界面支持简体中文 / English 双语，自建轻量 i18n（无第三方依赖）：

- `i18n/index.ts`：`t(text, vars?)` 翻译函数——中文原文作 key，英文从字典查，缺失回退中文；`{n}` 形式占位插值；
- `i18n/en/*.ts`：按域拆分的英文字典（chat / catalog / admin / config …），`en/index.ts` 合并导出；
- 语言偏好存 localStorage（`jx_lang`），切换语言整页 reload，保证模块级常量也按新语言重新求值；antd 组件经 `ConfigProvider locale` 跟随切换；
- 切换入口：主应用「设置 → 会话设置 → 界面语言」，管理后台右上角语言按钮；后端渲染的统一登录页同样内嵌双语字典并在右上角提供切换按钮，与前端共用同一 localStorage key；
- 边界：来自数据库的运行时内容（页面配置文案、服务配置元数据、用户数据）不做静态翻译，按存储语言原样展示。

## 数据流：一条消息的前端旅程

以发送一条消息为例，串起 store / hook / 组件三层的协作：

```
InputArea（components/chat/）
   │ 用户回车
   ▼
useStreaming.send
   │ 1. chatStore 追加乐观 user 消息
   │ 2. fetch POST /v1/chats/stream（携带能力位、附件、项目上下文）
   │ 逐行解析 data: {json}
   │ ├─ content/thinking → segments 分段追加（utils/segments.ts）
   │ ├─ tool_call/tool_result → 工具时间线（components/tool/）
   │ ├─ file_confirm/batch_confirm → 确认条 / 弹窗（真挂起）
   │ └─ meta → 引用源、产物列表写入消息
   ▼
chatStore 更新 → MessageBubble 重渲染
   │ markdown 渲染（utils/markdown.ts）+ 引用角标（components/citation/）
   ▼
[DONE] 收尾：落定消息状态，触发会话摘要 / 分类与追问问题拉取
```

断线恢复路径：`useChatInit` 启动时调 `getActiveChatRun` 探测进行中的 Run，命中则用 `followChatRun`（`GET /v1/chats/stream/{run_id}`）从偏移续播，`useStreaming` 复用同一套解析逻辑。

## 与版本（CE/EE）的关系

前端对版本的感知集中在两处：

- `editionStore` 启动时拉取 `/v1/meta/edition`（无鉴权探针），拿到 `{ edition, features }` 布尔表，EE 专属界面（团队、审计、计费入口等）据此显隐；
- `pageConfigStore` 承载品牌与文案配置（white-label），社区版保留「Powered by」署名，完全去署名为商业版能力（见 [版本与授权](../editions/overview.md)）。

## 构建与运行

- **开发**：`npm run dev`，Vite dev server 把 `/api` 代理到 `http://localhost:${BACKEND_PORT}`（`vite.config.ts`）；
- **生产**：`docker-compose up -d --build frontend`——多阶段镜像内 `npm run build`，产物交给 Nginx，`VITE_API_BASE_URL` 作为构建参数烘焙进 bundle（默认 `/api`，由 Nginx 反代到 backend 容器）；
- **快速热替**：本地 `npm run build` 后 `docker cp dist/. hugagent-frontend:/usr/share/nginx/html/` 并 reload Nginx（见 [前端开发指南](../development/frontend.md)）。

## 相关源码

| 主题 | 路径 |
|---|---|
| 入口分发 | `src/frontend/src/main.tsx` |
| 主应用壳 | `src/frontend/src/App.tsx` |
| API 客户端与信封解包 | `src/frontend/src/api.ts` |
| SSE 消费 | `src/frontend/src/hooks/useStreaming.ts` |
| 状态管理 | `src/frontend/src/stores/` |
| 组件组 | `src/frontend/src/components/` |
| 共享类型 | `src/frontend/src/types.ts` |
| 构建配置 | `src/frontend/vite.config.ts` |
| Nginx 配置 | `src/frontend/default.conf.template`、`src/frontend/nginx.conf` |
