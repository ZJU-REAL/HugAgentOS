# 对话与智能体编排

> 最后更新：2026-07-20

对话是 HugAgentOS 的核心链路：一条用户消息经过 FastAPI 路由、运行时上下文装配、流式编排器，最终由 AgentScope 2.0 的 ReActAgent 驱动多轮「思考 → 调工具 → 观察」循环，并以 SSE 事件流实时推送到前端。本篇按真实代码走一遍端到端流程，并展开引用系统、计划模式、子智能体、会话摘要、会话分享、上下文压缩与超长结果 offload 等子能力。

> 编排代码统一位于 `src/backend/orchestration/`（旧 `routing/` 目录已整体迁移至此）。

## 一次对话的端到端流程

```
浏览器 ── POST /v1/chats/stream ──▶ api/routes/v1/chats.py::chat_stream
   │   1. _ensure_main_model_configured()   主模型未配置直接 503
   │   2. 鉴权 / 会话归属校验 / 读取用户能力与记忆开关
   │   3. core/chat/context.py::build_runtime_context()  组装 workflow context
   ▼
orchestration/chat_run_executor.py::start_run()
   │   建 ChatRun 行 + 启动后台 asyncio.Task（与 HTTP 连接解耦）
   │   每个 chunk 转成 SSE 事件 XADD 到 Redis Stream jx:chat:run:{run_id}:events
   ▼
orchestration/workflow.py::astream_chat_workflow()
   │   ├─ orchestration/memory_integration.py  非阻塞记忆检索（后台 task + 预算超时）
   │   ├─ core/config/catalog_resolver.py      解析本次启用的 skills/mcp/kb
   │   ├─ core/llm/agent_factory.py::create_agent_executor()
   │   │     MCP 连接池 + 技能注册 + 文件工具 + 系统提示词 + 中间件 → Agent
   │   ├─ core/llm/context_manager.py          历史按 token 预算裁剪
   │   └─ orchestration/streaming.py::StreamingAgent.stream()
   │         消费 agent.reply_stream()，把 25 种细粒度事件映射为 8 类 SSE 事件
   ▼
SSE follower：chat_run_executor.follow_run_as_sse()
       XRANGE 重放 + XREAD 续播 → data: {...}\n\n → 浏览器
       （前端解析逻辑在 src/frontend/src/hooks/useStreaming.ts + App.tsx）
```

### Run 解耦与断线续播

每次发送消息会创建一条 `ChatRun` 并启动后台任务（`orchestration/chat_run_executor.py`），事件写入 Redis Stream（`maxlen=5000`，TTL 1 小时）。HTTP 连接只是"跟随者"，因此：

| 能力 | 端点 |
|---|---|
| 发起流式对话 | `POST /v1/chats/stream` |
| 刷新/断线后续播 | `GET /v1/chats/stream/{run_id}?from_offset=N` |
| 探测会话进行中的 run | `GET /v1/chats/{chat_id}/active-run` |
| 取消 run（真正杀后台任务） | `POST /v1/chat-runs/{run_id}/cancel` |

防御机制：静默 15 秒写一行 `: heartbeat` SSE 注释（防 nginx `proxy_read_timeout` 掐流）；workflow 600 秒无任何 chunk 触发看门狗判 failed（`CHAT_RUN_INACTIVITY_TIMEOUT_SEC`）；周期 reaper 把超龄 running run 收成 failed；启动钩子 `recover_orphan_runs()` 清理重启遗留。

### Agent 构建要点（core/llm/agent_factory.py）

`create_agent_executor()` 是所有模式（主对话、计划、批量、子智能体、自动化）共用的工厂：

- **MCP 工具**：经 catalog + 用户覆盖 + 请求上下文三层过滤后（见 [能力目录](catalog.md)），stable 服务复用进程级连接池（`core/llm/mcp_pool.py`），per-request 服务（如 `retrieve_dataset_content` 需带每请求 HTTP header）每次新建；用户自助添加的私有 MCP 按 owner 现查合入。
- **技能**：经 `core/agent_skills/loader.py` 注册为 AgentScope Agent Skills，并放行 `view_text_file` 读取 SKILL.md（详见 [技能系统](agent-skills.md)）。
- **文件/沙箱工具**：`bash`、`sandbox_put_artifact`、`sandbox_get_artifact` 无条件注册；Read/Edit/Write/Glob/Grep/Delete/Move/mkdir + MySpace 工具受 `CODE_CAPABILITY_ENABLED` 门控，共享同一个 `ReadStateTracker` 维持「先 Read 才能 Edit」不变量。
- **中间件**（洋葱模型，`core/llm/middlewares.py`）：`DynamicModelMiddleware`（按 chat_mode 切模型，见 [模型接入](model-providers.md)）、`FileContextMiddleware`（注入上传/历史文件上下文）、`WorkspacePinHintMiddleware`、`GoalAnchorReminderMiddleware`、`FinishPinGuardMiddleware`。
- **上下文压缩**：`ContextConfig(trigger_ratio=0.6, tool_result_limit=20000)` + 结构化中文「可恢复 ReAct 工作流」压缩提示词；压缩调用失败时由 `JxOpenAIChatModel.generate_structured_output` 返回 L3 占位摘要兜底。
- **权限**：所有已注册工具 seed 原生 `PermissionRule(ALLOW)`，保留 AgentScope 内置工具的危险操作检查（不使用一刀切 BYPASS）。
- **迭代上限**：主智能体默认 `max_iters=50`，隔离子智能体默认 10。

## SSE 事件类型与负载

`orchestration/streaming.py::StreamingAgent` 把 AgentScope 2.0 `reply_stream` 的细粒度事件归并为内部 8 类，`workflow.py` 与 `chats.py::_stream_sse_response` 再补充会话级字段后落到 wire 上。前端实际收到的事件：

| `type` | 含义 | 关键字段 |
|---|---|---|
| `thinking` | 思考过程（增量或阶段提示） | `delta` / `message` |
| `content` | 回答正文增量 | `event: "ai_message"`, `delta`, `chat_id` |
| `content_replace` | 本体评审修订了已流式展示的草稿时，原位替换最终答案 | `content`, `reason: "ontology_review"`, `chat_id` |
| `tool_call` | 一次工具调用（参数已完整） | `tool_name`, `tool_display_name`, `tool_args`, `tool_id`，调子智能体时附 `subagent_name` |
| `tool_result` | 工具调用结果 | `tool_name`, `result`, `tool_id`, `citations[]` |
| `subagent_event` | 子智能体内部过程，挂在父 `call_subagent` 卡片下 | `parent_tool_id`, `sub_type`, `agent_name`，以及内部工具或内容字段 |
| `ontology_activation` / `ontology_gate` / `ontology_review` | 本体治理状态，不属于模型思考 | 工作流、门禁决策、委员会状态与结论 |
| `tool_pending` | 工具已开始、参数仍在流式生成 | `tool_name` |
| `batch_confirm` | 批量计划生成完毕，等待用户确认（人审门） | `plan_id`, `total`, `preview`, `default_template`, `placeholder_keys` |
| `file_confirm` | 工具挂起等待用户确认「我的空间」写操作 | 确认上下文；用户带外 `POST /v1/chats/{chat_id}/file-confirm` 后工具原地续跑 |
| `meta` | 回合收尾元数据 | `route`, `citations[]`, `sources`, `artifacts`, `workspace_files`, `ontology_governance`, `warnings`, `is_markdown`, `message_id`, `usage` |
| `error` | 出错（已映射为用户友好中文文案） | `error`, `chat_id` |
| `heartbeat` | 心跳（事件级；另有 `: heartbeat` 注释行） | — |

流以 `data: [DONE]` 结束。示例帧：

```
data: {"type":"tool_call","tool_name":"internet_search","tool_display_name":"联网搜索","tool_args":{"query":"北京 集成电路 产业"},"tool_id":"call_abc"}

data: {"type":"tool_result","tool_name":"internet_search","result":{...},"tool_id":"call_abc","citations":[{"id":"internet_search-1","title":"...","url":"...","snippet":"...","source_type":"internet"}]}

data: {"type":"content","event":"ai_message","delta":"根据检索结果……","chat_id":"chat_x"}

data: {"type":"meta","route":"main","citations":[...],"usage":{"prompt_tokens":1234,"completion_tokens":456,"total_tokens":1690,"llm_call_count":3},"message_id":"msg_..."}

data: [DONE]
```

`meta` 之后，`chat_run_executor.py` 持久化助手消息、回填 artifact，并起后台任务生成追问问题（`orchestration/followups.py`，结果写进消息 `extra_data.follow_up_questions`，前端经 `GET /v1/chats/{chat_id}/messages/{message_id}/followups` 拉取）。本体事件在前端汇总为独立的“领域本体治理”模块，不再写入或显示在“思考过程”中。模型草稿保持逐 token 流式展示；委员会仅在实际修订答案时发送一次 `content_replace`，前端原位替换正文，数据库只保存评审后的最终答案。`ontology_governance` 随助手消息持久化，刷新历史会话后仍可回显。

## 引用系统（Citations）

引用让回答里的每个事实可溯源到具体工具结果，链路分三段：

1. **提示词约定**：系统提示词（`prompts/prompt_text/default/system/40_format.system.md` 的兜底版本，运行时以 DB 激活版本为准）要求模型引用工具数据时输出 `[ref:工具名-序号]` 标记，如 `[ref:internet_search-1]`、多来源并列 `[ref:tool1-N][ref:tool2-M]`。
2. **后端抽取**：每个 `tool_result` 事件经 `orchestration/citations.py` 归一化为 `CitationItem`（`id` / `tool_name` / `tool_id` / `title` / `url` / `snippet` / `source_type`）。同一回合内同一工具被多次调用时，`extract_citations_with_offset()` 用 per-turn 偏移表保证 id 不重复。`source_type` 取值由 `_SOURCE_TYPE_MAP` 决定：`internet`、`knowledge_base`、`database`、`industry_news`、`ai_news`、`chain_info`、`company_profile`（后三类来自行业工具，**商业版 EE**）。
3. **前端渲染**：citations 随 `tool_result` 与 `meta` 事件下发并随消息持久化；`src/frontend/src/utils/citations.ts` 用 `/\[ref:([\w]+-\d+)\]/g` 解析正文标记，`components/citation/CitationBadge.tsx` 渲染为可点击角标，`CitationMarkdownBlock` / `CitationHtmlBlock` 负责正文内嵌展示。

## 计划模式（Plan Mode）

计划模式把复杂任务拆成「生成计划 → 用户确认/编辑 → 逐步执行」两阶段，实现在 `orchestration/subagents/plan_mode.py`：

- **生成**（`astream_generate_plan` / `POST /v1/plans/generate`）：以 `disable_tools=True` 的"裸模型"产出结构化 JSON 计划。系统提示词解析顺序：版本池 `plan_mode` 激活版本 → 旧版 `system/90_plan_mode` 分段 → 文件兜底 `prompts/prompt_text/plan_mode/plan_mode.system.md` → 硬编码最小提示。
- **执行**（`astream_execute_plan` / `POST /v1/plans/{plan_id}/execute`）：每个步骤独立建 agent 顺序执行，支持步骤级 MCP/技能/子智能体绑定、取消（`is_run_cancelled` 轮询）；执行同样走 ChatRun + Redis Stream，可断线续播。
- **模型角色**：计划模式优先解析 `plan_agent` 角色，未配置降级 `main_agent`（`agent_factory.py` `_mode_role` 分支）。
- 无人值守模式（计划执行 / 自动化）会从工具集中摘除 `batch_runner`，因为 `batch_plan` 的确认弹窗在该场景无 UI 可确认（`workflow.py::_resolve_batch_runner_visibility`）。

## 子智能体（Sub-agents）

用户自建子智能体（`api/routes/v1/agents.py`，DB 表 `UserAgent`）可绑定独立的系统提示词、MCP / 技能 / 插件 / KB 集合与模型参数（provider / temperature / max_tokens / max_iters）。创建或编辑时，资源选择器支持以下来源：

- 已安装的技能与插件；
- 技能市场和插件市场，安装完成后自动绑定到当前子智能体；需要凭据的市场资源仍先走原有凭据配置与安装权限校验；
- 当前用户未启用、但管理员仍允许使用的 MCP。该绑定只对当前子智能体生效，不会同步开启主智能体的个人能力开关；管理员全局停用的 MCP 仍不可绑定。

子智能体有四种触达方式，编排归属取决于用户是否明确指定了目标：

- **结构化 `@` 委派**：从输入框选择一个 `@子智能体` 时，前端同时提交
  `mention_agent_id` 和显示名。后端移除仅用于展示的 `@名称` 前缀，并向当前用户回合注入严格委派
  约束；主模型仍保留正常思考和逐 token 输出，其下一个真实工具调用必须是目标智能体的
  `call_subagent`，不得先自行查询数据。子智能体的完整执行均发生在该工具内部，其思考、工具和正文
  作为 `subagent_event` 挂在真实工具卡片下，返回后主模型继续流式整合答案。该回合保持 `main`
  路由，且不会把普通会话永久绑定为子智能体会话。旧客户端只提交 `mention_name` 时，后端仅在名称
  唯一且可访问时兼容解析。
- **自然语言显式委派**：以“调用”或“请调用”开头，并包含唯一、完整的可访问子智能体名称和
  明确动作任务时，后端只解析目标并向当前用户回合注入委派约束，不会伪造工具事件或绕过主模型。
  主模型保留正常思考和流式链路，其下一个真实工具调用必须是目标智能体的 `call_subagent`，不得在此之前调用其他工具。
  例如，`调用企业风险分析子智能体 分析杭州量知的风险` 会在模型发出真实调用时显示 `call_subagent` 卡片，
  子智能体的思考和内部工具作为 `subagent_event` 挂在该卡片下，返回后由主模型继续流式整合最终回答。
  该回合仍是 `main` 路由，`call_subagent` 及内部工具各自保留真实审计日志。名称重复、目标已停用、任务为空，
  或者“调用企业风险分析子智能体是否合适？”这类讨论句不会触发强制委派。
- **专属会话**：从子智能体详情页进入的会话使用 `agent_id`，后续轮次持续由该子智能体执行。
- **主智能体自主编排**：既没有结构化 `@`，也没有命中严格自然语言调用语法时，主智能体可
  按任务需要调用
  `core/llm/subagent_tool.py` 注册的 `call_subagent`。子智能体在独立线程和事件循环中运行，结果
  回传给主智能体整合。这一路径适合多子智能体并行、任务拆分和跨领域汇总。

## 会话摘要与上下文压缩

三个层次互补：

| 层次 | 实现 | 触发 |
|---|---|---|
| 会话标题摘要 | `core/llm/summarizer.py::ConversationSummarizer`（`summarizer` 模型角色，`ENABLE_SUMMARY` 开关），`POST /v1/summary` | 新会话标题自动生成 |
| 历史预裁剪 + 摘要 | `core/llm/context_manager.py::ContextWindowManager.manage_context()` 按模型上下文窗口裁剪；被裁掉的旧消息经 `core/llm/history_summarizer.py::summarize_history()` 压成 `<conversation_summary>` 注入队首 | 加载历史超出 token 预算时 |
| 会话内压缩 | AgentScope 2.0 `ContextConfig`（`trigger_ratio=0.6`），压缩提示词要求产出可恢复 ReAct 工作流的结构化摘要（保留 artifact_id、工具参数、待办） | ReAct 循环内上下文逼近窗口时 |

## 超长工具结果 offload

`core/llm/offloader.py::SandboxOffloader` 实现 AgentScope 2.0 `Offloader` 协议：上下文压缩/工具结果截断时，溢出部分不再被静默丢弃，而是落盘到沙箱 `/workspace/.offload/`（`tool_<id>.txt` / `context_<hash>.txt`），框架会在给模型的 `<system-reminder>` 里附上路径，模型可用 `Read` / `bash` 按需读回。仅在沙箱工具启用（`SANDBOX_TOOLS_ENABLED=true`，默认开）时挂载，写失败永不抛异常、返回降级说明。

## 会话分享（Chat Shares）

`api/routes/v1/chat_shares.py` 提供只读分享链接：

| 端点 | 说明 |
|---|---|
| `POST /v1/chat-shares` | 选定消息生成分享链接，有效期 `3d / 15d / 3m / permanent` |
| `GET /v1/chat-shares` | 当前用户的分享记录 |
| `GET /v1/chat-shares/{share_id}` | 匿名访问分享内容（含过期判定） |
| `POST /v1/chat-shares/{share_id}/revoke` / `restore` | 终止 / 恢复访问 |
| `DELETE /v1/chat-shares/{share_id}` | 删除记录 |

存储走 Redis（`chat_share:*` 三组 key + TTL），Redis 不可用时降级为进程内存（仅适合开发环境）。会话在**团队项目**内的共享范围由 `POST /v1/chats/{chat_id}/share` 单独管理（**商业版 EE**，依赖团队体系）。

## 其它入口

同一编排底座还服务：消息重新生成（`POST /v1/chats/{chat_id}/regenerate`）、编辑重发（`POST /v1/chats/{chat_id}/edit`）、非流式 `POST /v1/chats/send`、批量执行（`orchestration/batch_orchestrator.py`，见 [自动化](automation.md)）与定时自动化（`orchestration/schedulers/`）。

## 相关源码

| 主题 | 路径 |
|---|---|
| 聊天路由 / SSE 出口 | `src/backend/api/routes/v1/chats.py` |
| Run 解耦 / Redis Stream / 续播 | `src/backend/orchestration/chat_run_executor.py`，`api/routes/v1/chat_runs.py` |
| 流式编排主流程 | `src/backend/orchestration/workflow.py` |
| 事件映射（reply_stream → SSE） | `src/backend/orchestration/streaming.py` |
| 运行时上下文装配 | `src/backend/core/chat/context.py` |
| Agent 工厂 | `src/backend/core/llm/agent_factory.py` |
| 中间件 | `src/backend/core/llm/middlewares.py`（纯函数 helper 在 `core/llm/hooks.py`） |
| 引用抽取 | `src/backend/orchestration/citations.py` |
| 引用前端渲染 | `src/frontend/src/utils/citations.ts`，`src/frontend/src/components/citation/` |
| 计划模式 | `src/backend/orchestration/subagents/plan_mode.py`，`api/routes/v1/plans.py` |
| 子智能体工具 | `src/backend/core/llm/subagent_tool.py`，`api/routes/v1/agents.py` |
| 标题摘要 / 历史摘要 / 窗口管理 | `src/backend/core/llm/summarizer.py`，`history_summarizer.py`，`context_manager.py` |
| 超长结果 offload | `src/backend/core/llm/offloader.py` |
| 会话分享 | `src/backend/api/routes/v1/chat_shares.py` |
| 追问生成 | `src/backend/orchestration/followups.py` |
| 前端流式解析 | `src/frontend/src/hooks/chatStream.ts` |
