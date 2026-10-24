# hugagent 终端客户端设计（对标 Codex CLI / pi）

状态：草案，未实现。已定取舍：TUI 用 TypeScript + `@mariozechner/pi-tui`；local 模式复用现有后端运行时。

## 1. 目标

- 交互体验对标 pi：内联差分渲染（保留终端原生回滚，不占全屏），多行编辑器，流式 Markdown，工具调用一行一条，运行中可插话。
- 能力对标 Codex CLI：审批与沙箱档位、会话恢复、非交互 `exec`、JSONL 事件输出、MCP、`/review` `/diff`。
- `/mode` 切换执行位置：
  - `local`：全部是本机进程，agent 直接操作当前目录。
  - `cloud`：连接远端 HugAgentOS 服务。
  - `device:<id>`：远程运维另一台已配对的电脑。
- 两种模式用同一套界面、同一套事件协议，切换后体验一致。

不做：在 TypeScript 里重写 agent 循环。agent、工具、沙箱、压缩、技能、记忆全部由后端负责。

## 2. 架构

```
┌──────────────── hugagent（Node，packages/cli）───────────────┐
│  入口：interactive │ -p/exec │ --output json │ --output rpc   │
│  TUI：pi-tui（Editor / Markdown / SelectList / Loader）       │
│  Engine 接口 ── send / subscribe / steer / cancel / 审批 / 会话 │
│     ├─ LocalEngine  → http://127.0.0.1:<随机端口>（本机子进程）  │
│     └─ RemoteEngine → https://<server>（cloud / device:<id>）   │
└──────────────────────────────┬───────────────────────────────┘
                               │ 同一套 HTTP + SSE（@hugagent/chat-sdk）
┌──────────────────────────────┴───────────────────────────────┐
│ Python 后端（现有）：agent 运行时 · 工具 · OS 沙箱 · 压缩 · 技能 · MCP │
│ local：hugagent-server engine（SQLite，~/.hugagent，绑定 loopback）│
└──────────────────────────────────────────────────────────────┘
```

### 为什么本机引擎走 loopback HTTP，而不是 stdio RPC

调整说明：讨论取舍时写的是 stdio JSON-RPC，这里改成 loopback HTTP。理由如下：

- 后端已有完整的 SSE 事件协议，包括 `run_snapshot` 断线续播、`run.steer` 插话，以及 `tool-approval`、`file-confirm`、`user-questions` 带外审批。`@hugagent/chat-sdk` 已经封装了 `send / resume / subscribe`。走 HTTP，local 和 cloud 之间只差 base URL 和凭据。
- 如果改成 stdio RPC，就要为同一套能力再定义第二种协议，两边会慢慢分叉。
- 桌面端已经在 32101 端口跑着本机后端。CLI 检测到它（并且数据目录相同）时可以直接接入，和桌面端共享会话、不再重复启动。
- pi 风格的 `--output rpc` 仍然保留，但它是 CLI **对外**提供的嵌入协议，不是 CLI 和后端之间的协议。

### LocalEngine 生命周期

1. 先找可复用的后端：读 `~/.hugagent/engine.json`（端口、令牌、PID），健康检查通过就直接接入。
2. 找不到就启动 `hugagent-server engine --port 0 --token-file …`：
   - 无头运行，不开浏览器、不托管前端静态文件；
   - 只绑定 127.0.0.1；
   - 就绪后把端口写进就绪文件。
3. 鉴权：一次性随机令牌，复用桌面桥的机制（`HUGAGENT_DESKTOP_BRIDGE_SECRET` 这一类）；不需要本机账号密码。
4. 引擎空闲 N 分钟后自动退出；CLI 退出不强制关闭它，下次启动就是热启动。
5. 当前目录映射：按 realpath 查找或创建 `kind=local` 的项目，挂载到 `/workspace/local/<slug>`（`core/sandbox/workspace/mounts.py`）。
   - 新会话都建在这个项目里，根目录的 `AGENTS.md` 由现有逻辑读取（`core/projects/instructions.py`）。
   - 命令执行用现有 OS 沙箱：Seatbelt / bwrap / Windows 低权限令牌。
6. 首次使用时没有模型配置：TUI 内引导完成（复用 `cli.py` 的 `configure_model` 逻辑），不跳到网页。

### RemoteEngine

- `cloud`：用 `/login` 获取个人 API Key 并存进系统钥匙串，回退到 `~/.hugagent/cli.json`（权限 0600）。默认要求 HTTPS，只有 loopback 和显式允许的内网地址例外。
- 云端 agent **碰不到本机当前目录**。状态栏必须常驻显示 `cloud · 无本地文件访问`，`@文件` 只能以附件上传的方式提供。
- `device:<id>`：沿用现有 session-manager 操作队列（`hugagent_cli/contract.py`）。这台设备上的对话由那台机器执行，TUI 只能轮询结果，不能实时流式显示（设备链路目前没有 SSE）。

## 3. `/mode` 语义

| 操作 | 行为 |
|---|---|
| `/mode` | 弹出选择列表：local / cloud / 在线设备，并标出当前项 |
| `/mode local\|cloud\|device:<id>` | 直接切换。当前会话留在原来那一侧，目标侧开新会话；`/resume` 只列目标侧、当前项目的会话 |
| `--mode` 启动参数 / 配置文件 `defaultMode` | 启动时的默认值；不指定时，如果当前目录是 git 仓库就用 local，否则用上次的选择 |
| 状态栏 | `local · ~/repo · deepseek-chat · 审批:按需 · ctx 42%` |

- 第一版不支持把会话从一侧搬到另一侧。以后可以加 `/handoff`：把摘要和关键文件带到另一侧，开一个新会话。
- 命名提醒：pi 的 `--mode` 表示输出格式。我们的 `--mode` 表示执行位置，输出格式统一改用 `--output text|json|rpc`。

## 4. 功能对标矩阵

Codex / pi 两列按公开版本归纳，细节以官方文档为准。"后端"列表示后端现在是否已经具备这项能力。

| 能力 | Codex | pi | 我们的做法 | 后端 | 阶段 |
|---|---|---|---|---|---|
| 内联差分渲染、原生回滚 | 全屏 TUI | ✓ | pi-tui | — | P1 |
| 多行编辑器、历史、快捷键 | ✓ | ✓ | pi-tui Editor | — | P1 |
| 流式 Markdown、思考折叠 | ✓ | ✓ | 渲染 `content` / `thinking` 事件 | ✓ | P1 |
| 工具调用一行显示、可展开 | ✓ | ✓ | `tool_call_*` / `tool_result` / `subagent_event` | ✓ | P1 |
| Esc 中断 | ✓ | ✓ | `run.cancel` | ✓ | P1 |
| 运行中插话 / 排到下一条 | 排队 | Enter 插话 / Alt+Enter 排队 | `run.steer`，`delivery_mode=steer\|followUp\|nextRun` | ✓ | P1 |
| 工具与命令审批 | approval policy | 默认不拦 | 终端内回应 `tool-approval` / `file-confirm` | ✓ | P1 |
| 向用户提问 | — | — | `user_question` 内联选择 | ✓ | P1 |
| 沙箱档位 `/permissions` | read-only / workspace-write / full | — | 映射现有权限预设 + OS 沙箱 | ✓ | P2 |
| `/model`、思考档位 | ✓ | ✓（Ctrl+P / Shift+Tab） | `ChatRequest.model_name / model_provider_id`，思考档位走偏好设置 | ✓ | P1 |
| 会话恢复 `-c` / `-r` | `resume` | ✓ | 服务端对话按项目（当前目录）过滤 | ✓ | P1 |
| 分支 `/fork`、`/tree`、回到旧消息重写 | Esc Esc 回退 | ✓ | `POST /chats/{id}/fork` | ✓ | P2 |
| 手动 `/compact` | ✓ | ✓ | 后端只有自动压缩，**需新增接口** | 部分 | P2 |
| 上下文用量显示 | ✓ | ✓ | `context_usage` 事件 | ✓ | P1 |
| AGENTS.md | 逐级向上 + 全局 | 逐级向上 + 全局 | 只读项目根目录，**需补逐级合并和全局文件** | 部分 | P2 |
| `@文件` 模糊引用 | ✓ | ✓ | local：路径交给 agent；cloud：作为附件上传 | 部分 | P2 |
| `!命令` 直接执行 | ✓ | ✓ | local 在本机沙箱执行，把输出注入上下文 | — | P2 |
| 粘贴图片 | ✓ | ✓ | 附件 + 视觉桥 | ✓ | P2 |
| `/diff`、`/review` | ✓ | 扩展实现 | local 由客户端读 git；review 作为一种对话模式 | — | P2 |
| 非交互 `exec` / `-p` | ✓ | ✓ | `hugagent -p "…"`，退出码反映运行结果 | ✓ | P0 |
| JSONL 事件流 | `--json` | `--mode json` | `--output json`，原样输出 SSE 事件 | ✓ | P0 |
| 嵌入协议 | app-server | `--mode rpc` | `--output rpc`（stdin/stdout JSON） | — | P2 |
| MCP | ✓ | 不内置 | 后端插件市场；提供 `hugagent mcp list/add` | ✓ | P3 |
| 技能 / 提示模板 / 扩展 | ✓ | ✓ | 后端技能 + 插件；TUI 侧扩展暂不做 | ✓ | P3 |
| 登录 | ChatGPT / API Key | 订阅 / API Key | `/login`（cloud）；local 的模型凭据存在本机库 | 部分 | P1 |
| 主题、按键绑定 | 部分 | ✓ | `~/.hugagent/cli.json` | — | P3 |
| 导出 / 分享 | — | HTML / gist | `/export` Markdown 或 HTML | — | P3 |
| 远程运维 | — | — | `/mode device:<id>`、`/sessions`、`/runs`、`/devices` | ✓ | P3 |
| 子 agent、计划、自主循环 | 部分 | 扩展 | 渲染 `subagent_event` / `plan_update`（我们的优势） | ✓ | P2 |
| 分发、升级 | npm / brew | npm | npm 全局安装；local 引擎复用桌面端自带的 Python 运行时，或 `pip install hugagent` | — | P3 |

## 5. 后端需要补的东西

1. `hugagent-server engine` 子命令：无头运行、`--port 0`、就绪文件、loopback 令牌鉴权、空闲退出、父进程监护。
2. 按路径查找或创建本地项目的接口（目前只有桌面端授权文件夹的流程）。
3. 手动压缩接口，对应 `/compact`。
4. `ChatRequest.message` 的长度上限 10000 字符，对粘贴代码太小。附件可以绕开，但 `-p` 和管道输入需要更大的上限，或者自动转成附件。
5. AGENTS.md 逐级向上合并 + 全局文件 `~/.hugagent/AGENTS.md`。
6. 统一命名：
   - Python 的 console script 改名 `hugagent-server`（负责 onboard / serve / doctor / engine）；
   - npm 包占用 `hugagent`；`hugagent serve` 等命令转发给 `hugagent-server`；
   - 云电脑镜像里的零依赖会话客户端 `hugagent_cli` 保持不变，因为 Personal Agent 依赖它。
   - 上次审查发现的入口路由、错误详情丢失、空响应、操作表清理等问题，在这一步一起修。

## 6. 包结构

```
packages/cli/
  src/
    main.ts              参数解析，分发到各模式
    config.ts            ~/.hugagent/cli.json、钥匙串
    engine/
      engine.ts          Engine 接口 + 统一事件类型（复用 chat-sdk 类型）
      local.ts           发现 / 启动 / 接入本机引擎，当前目录 → 本地项目
      remote.ts          cloud 与 device:<id>
    modes/
      interactive.ts     TUI 主循环
      print.ts           -p / exec
      json.ts            --output json
      rpc.ts             --output rpc
    tui/
      transcript.ts      事件 → 组件（正文 / 思考 / 工具行 / 计划 / 子 agent）
      footer.ts          状态栏
      prompts/           审批、提问、选择列表
      commands/          /mode /model /resume /new /compact /permissions /fork /diff …
  test/
```

## 7. 分期与验收

- **P0 骨架**：包结构、两种 Engine、`-p` 和 `--output json`。
  - 验收：在任意仓库目录执行 `hugagent -p "列出这个目录的文件"`，local 模式由本机引擎在当前目录完成；`--mode cloud` 走远端服务；两边的 JSONL 事件一致。
- **P1 交互 TUI**：编辑器、流式渲染、工具行、状态栏、Esc 中断、插话 / 排队、审批与提问内联处理、`/mode` `/model` `/new` `/resume` `/login` `/quit`。
  - 验收：一次完整的"改代码 → 审批 → 跑测试"闭环不离开终端。
- **P2 对标补齐**：`@文件`、`!命令`、图片、`/compact`、`/permissions`、`/fork` `/tree`、`/diff` `/review`、AGENTS.md 层级、`--output rpc`。
- **P3 生态与运维**：`device:<id>`、`/sessions` `/runs` `/devices`、MCP / 技能命令、主题按键、导出、npm 发布与升级。

## 8. 风险

- **Python 引擎冷启动慢**：完整 lifespan 要数秒。先实测；用"接入已运行的后端 + 空闲保活"缓解；engine 子命令只启动 agent 运行需要的步骤。
- **两套运行时**：只用 cloud 的用户只需要 Node；local 需要 Python 运行时，优先复用桌面端已安装的那份。
- **pi-tui 还是 0.x**：锁定精确版本，用自己的组件层隔离它的 API。
- **终端兼容**：中文输入法和宽字符（pi-tui 依赖 get-east-asian-width），Windows Terminal，SSH 下的剪贴板。
- **cloud 不能访问本机目录**：必须在界面上一直说清楚，否则用户会以为云端改了本地代码。
