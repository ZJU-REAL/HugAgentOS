# 前端架构详解

> 最后更新：2026-10-18

前端位于 `src/frontend/`，是一个 React 19 + TypeScript 单页应用：Vite 构建、Ant Design 组件库、Zustand 状态管理、主应用使用 React Router，按 URL 路径在入口处切换五个独立应用壳。生产环境构建产物由 Nginx 容器托管，所有后端调用收敛到一个类型化客户端 `api.ts`。

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

## 模块导航与职责拆分

主界面采用模块图标栏、当前模块侧栏和内容区三层布局。对话模块只展示普通会话与项目会话；能力中心侧栏依次选择智能体、插件、技能、连接器，已安装能力及其操作位于右侧内容区；我的空间侧栏管理文件、知识库、收藏、分享和通知。定时任务的创建会话和执行会话均留在定时任务模块，搜索、通知、刷新及历史导航复用同一会话地址。

App.tsx 只连接 useAppController、AppAuthGate 和 AppWorkspace。初始化、流恢复、搜索和滚动分别由 useAppBootstrap、useChatRecovery、useChatSearch、useChatViewport 管理；侧栏按模块菜单、会话行、项目分组和账号工具拆分；我的空间按控制器、文件夹动作、知识库选择、页头、内容和弹窗拆分。路由是模块位置的唯一来源，automationStore 是任务列表的唯一来源。

## 模块页面滚动与应用中心

普通模块页面由 AppWorkspace 的公共内容区管理纵向滚动。滚动视口覆盖整个可用页面宽度，滚动条位于该页面的最右缘。项目卡片、我的空间文件列表和应用卡片各自的最大宽度只用于内容排版，不再创建主页面滚动容器。切换模块地址后回到页首；我的空间的页头保持吸顶。聊天、Canvas 和插件工作区保留各自的专用布局，弹窗、代码编辑器和限高预览仍可管理内部滚动。

公共布局样式位于 `styles/workspace-layout.css`。我的空间与定时任务样式按布局、文件、内容、详情、创建和响应式职责拆为小文件，聚合入口只负责导入。新增模块不得在限宽阅读列内设置整页 `height:100%` 与 `overflow:auto`。

应用中心恢复图标、名称和说明的卡片宫格，只展示计划模式等命令应用、插件应用和外部场景应用。导航模块不属于应用中心。卡片仍通过服务端目录和启动校验决定可见性、可用状态及打开方式，不恢复旧的 URL 票据拼接或已退役批量执行。

运行 `npm run test:module-pages`，检查九个模块在经典/现代、宽屏/中屏/窄屏下的滚动几何、实际滚轮操作、居中阅读宽度，以及应用卡片和计划模式键盘启动。该命令还运行聊天与 Canvas 宽度回归。`npm run test:sidebar-navigation` 继续验证更多菜单和二级边栏。

## api.ts — 类型化 API 客户端

`src/api.ts`是唯一的后端出入口：

- **基础地址**：`getApiUrl()` 返回 `import.meta.env.VITE_API_BASE_URL || '/api'`——开发态走 Vite 代理，生产态走 Nginx 同源 `/api`；
- **信封解包**：后端统一返回 `{ code, message, data, trace_id, timestamp }`，`isApiEnvelope` + `unwrapData<T>` 自动取出 `data`，调用方拿到的就是业务类型；
- **认证联动**：`onUnauthorized(handler)` 注册 401 回调，由 `authStore` 统一跳转登录；
- **错误友好化**：如 `uploadErrorMessage` 识别 Nginx 413（超出 `client_max_body_size`）等非 JSON 错误并给出中文提示；
- **领域函数**：会话 CRUD / 流式与续播 / 消息反馈 / 知识库 / 产物 / 自动化 / 项目 / 记忆 / 能力中心等数百个具名函数，与 `types.ts` 中的共享类型一一对应。

SSE 流式不走 `api.ts` 的 JSON 通道，由 `hooks/useStreaming.ts` 直接消费 `fetch` 流。

## 组件组（components/ 下 22 组）

| 组件组 | 职责 |
|---|---|
| `admin/`（商业版 EE） | 内容管理台面板：技能 / 知识库 / 智能体管理器、页面配置编辑器、图标选择器等 |
| `agent/` | 子智能体的创建页、表单、@提及弹层、面板 |
| `apidoc/` | API 文档页面板 |
| `automation/` | 定时任务列表、创建、详情、创建会话入口与执行时间线 |
| `canvas/` | 数据画布：Univer 在线表格面板 |
| `catalog/` | 能力中心：技能 / MCP 页面、目录面板、技能市场弹窗、图标选择 |
| `chat/` | 聊天主区：消息气泡、输入区、产物卡片、思考面板、计划卡片、技能斜杠弹层、文件确认条等 |
| `citation/` | 引用角标与引用感知的 Markdown / HTML 渲染块 |
| `common/` | 通用件：加载骨架、登录过期弹窗、品牌 Loader、计时器、图片预览 |
| `config/`（商业版 EE） | 系统管理台面板：聊天历史审查、注册码、license、沙箱实例、安全审计日志等 |
| `docs/` | 应用中心与文档面板（版本说明等） |
| `file/` | 附件卡片、文件预览窗格、我的空间导入弹窗 |
| `kb/` | 知识库创建与重建索引弹窗 |
| `lab/` | 实验室：技能蒸馏与自主循环 |
| `memory/` | 记忆事实列表 |
| `myspace/` | 我的空间：文档 / 收藏 / 图片 / 通知列表与主面板 |
| `projects/` | 项目工作空间：卡片、详情面板、右栏、记忆弹窗 |
| `settings/` | 设置弹窗、个人 API-Key 面板、团队区块 |
| `share/` | 分享记录页 |
| `sidebar/` | 侧边栏、全局搜索弹窗、导航项定义 |
| `tool/` | 工具调用时间线：调用行、思考行、输出渲染器、结果面板、进度内联 |

## Zustand 状态（stores/）

| Store | 职责 |
|---|---|
| `chatStore` | 会话列表、当前会话、消息流（核心状态） |
| `authStore` | 登录态、当前用户、401 跳转与登录落地页 |
| `uiStore` | 全局 UI：面板开合、当前视图、弹窗状态 |
| `catalogStore` | 能力目录（技能 / 智能体 / MCP / KB）及启停 |
| `agentStore` | 子智能体列表与选中态 |
| `settingsStore` | 用户设置（记忆开关、模型偏好等） |
| `fileStore` | 上传附件与上传状态 |
| `kbStore` | 知识库空间 / 文档 / 分块状态 |
| `mySpaceStore` | 我的空间资源树与收藏 |
| `projectStore` | 项目列表、详情、项目内会话 |
| `automationStore` / `automationChatStore` | 自动化任务列表 / 自动化会话运行态 |
| `canvasStore` | 数据画布文档状态 |
| `skillDistillStore` | 个人技能蒸馏任务状态 |
| `pageConfigStore` | 页面配置（品牌、导航、文案——驱动 white-label） |
| `editionStore` | `/v1/meta/edition` 探针的消费端：版本与 license 能力位布尔表 |
| `modelCapabilitiesStore` | 主模型能力探测（思考 / 视觉等） |
| `sidebarOrderStore` | 侧边栏对话列表的手动拖拽顺序（本地 + `users_shadow.metadata` 双层持久化） |

## Hooks

| Hook | 职责 |
|---|---|
| `useStreaming` | SSE 主消费器：暴露 `send` / `abort` / `regenerate` / `editAndResend` / `resumeRunIfAny`，解析 `content/thinking/tool_call_start/tool_call_delta/tool_call/tool_result/tool_progress/meta/error` 事件，维护文本分段与工具时间线，支持 run 续播 |
| `useChatActions` | 会话管理动作封装：新建 / 删除 / 重命名 / 置顶收藏 / 导出 / 分享 / 摘要与分类 |
| `useChatInit` | 应用启动时的会话初始化与活动 run 恢复 |
| `usePlanMode` | 计划模式 SSE 消费器（首次执行与续播共用） |
| `usePageConfig` | 按 dot-path 读取页面配置（如 `branding.product_name`） |
| `useStallDetector` | 流静默超时检测（卡顿提示） |
| `useDelayedFlag` | 防闪烁的延迟布尔量（延迟显示 + 最短保持） |

## 流式正文显示

助手正文从首帧开始按 Markdown 渲染。普通段落、格式化正文和带引用正文共用 CitationHtmlBlock，不再根据后续字符切换纯文本与 Markdown 容器。SSE 解析和后台任务仍负责接收与恢复数据；GraphemeReveal 只负责显示新增文字。

新增文字按完整字素淡入，组合重音和 ZWJ emoji 不按 UTF-16 单元拆开。动画参数在 styles/chat/grapheme-reveal.css 中统一定义：90ms、cubic-bezier(.215,.61,.355,1)、模糊从 6px 到 0；正常字素间隔为 30ms。大批增量会缩短显示间隔，将待显示时间限制在 600ms 内。已显示文字和引用 portal 的节点保持原位；动画完成后合并临时文字节点，避免长消息保留逐字 DOM。

代码、公式、复制按钮和图表组件不拆分。完成或取消时，页面立即显示全部已接收文字。恢复快照中的已有文字不重播；后续增量继续淡入。系统启用减弱动画时直接显示正文。

Markdown 转 HTML 后复用 DOMPurify 清理可执行属性和链接，同时保留外链的新标签页行为。

验证入口为 npm run test:grapheme-reveal。完整应用测试使用 scripts/grapheme-e2e-environment.py 创建独立 Docker 网络、Redis、临时 SQLite、测试模型和 Nginx，再运行 npm run test:grapheme-reveal-app。测试环境不读取真实 .env；完成后用该脚本的 down 命令移除带测试标签的容器。

## 样式与其他目录

- `styles/`：按功能域拆分的全局 CSS（`chat.css`、`catalog.css`、`automation.css`、`canvas.css`、`myspace.css`、`projects.css`、`config.css`、`team-folder.css` 等），`variables.css` 收口设计变量，`index.ts` 统一引入；
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
   │ 2. fetch POST /v1/agents/responses（附件只携带 file_id/name/mime_type）
   │ 逐行解析 data: {json}
   │ ├─ content/thinking → segments 分段追加（utils/segments.ts）
   │ ├─ tool_call_start/tool_call_delta/tool_call/tool_result → 按 tool_id 原位更新工具时间线（components/tool/）
   │ ├─ file_confirm → 确认条 / 弹窗（真挂起）
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

## 会话状态标记

会话运行时显示灰色旋转圆环，完成但结果未读时显示蓝色呼吸灯。读取结果后清除完成标记。等待输入或确认、失败继续使用黄色和红色提示。项目分组中的会话将标记放在标题左侧的固定缩进槽，标题位置不随状态变化；默认项目（历史对话）将标记放在右侧。运行圆环遵循工作指示器动效规则，减弱动画时仍保留旋转；未读呼吸灯降级为静态蓝点。

## 首条问题的异步标题

普通会话的首条问题接纳后，服务端先保留基于问题的截取标题，再独立调用摘要模型，只传入首条用户问题。前端在回答流进行期间读取标题结果；临时标题不会阻止生成。每个会话只自动尝试一次，失败保留兜底标题，后续问题不重复生成；fork 会话保留继承标题。显式标题修改会在服务端固定名称，模型完成时再次核验，防止并发覆盖。标题更新不改变会话顺序，也没有专用的文字过渡动效。

### 消息身份与登录续期

发送接口在 SSE 响应头 `X-Hugagent-User-Message-Id` 中返回本次已保存的用户消息 ID，跨源请求可读取该响应头。前端在消费回复流前，将该 ID 绑定到本次本地消息；如果历史请求已返回同一记录，则按 ID 合并。内容相同的不同发送仍保留各自的消息 ID，不按正文或时间去重。

历史加载依赖用户 ID 和会话 ID，不依赖整个登录用户对象。登录续期继续更新过期时间、资料和权限；仅这些字段变化不会重新加载正在生成回复的会话历史。
