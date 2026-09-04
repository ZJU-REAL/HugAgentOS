# 对话与智能体编排

> 最后更新：2026-08-26

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
   │         消费 agent.reply_stream()，归并 25 种细粒度事件并保留工具参数增量
   ▼
SSE follower：chat_run_executor.follow_run_as_sse()
       XRANGE 重放 + XREAD 续播 → data: {...}\n\n → 浏览器
       （前端解析逻辑在 src/frontend/src/hooks/useStreaming.ts + App.tsx）
```

### Run 解耦与断线续播

每次发送消息会创建一条 `ChatRun` 并启动后台任务（`orchestration/chat_run_executor.py`），事件写入 Redis Stream（`maxlen=5000`；默认 TTL 为最长人机等待 7200 秒加 1800 秒恢复余量）。HTTP 连接只是"跟随者"，因此：

| 能力 | 端点 |
|---|---|
| 发起流式对话 | `POST /v1/chats/stream` |
| 刷新/断线后续播 | `GET /v1/chats/stream/{run_id}?from_offset=N` |
| 探测会话进行中的 run | `GET /v1/chats/{chat_id}/active-run` |
| 取消 run（真正杀后台任务） | `POST /v1/chat-runs/{run_id}/cancel` |
| 在下一次安全 ReAct 边界追加指令 | `POST /v1/chat-runs/{run_id}/steer` |
| 查询耐久追加队列状态 | `GET /v1/chat-runs/{run_id}/steers` |
| 撤回尚未生效的追加指令 | `DELETE /v1/chat-runs/{run_id}/steer/{steer_id}` |

防御机制：静默 15 秒写一行 `: heartbeat` SSE 注释（防 nginx `proxy_read_timeout` 掐流）；workflow 600 秒无有效输出触发看门狗判 failed（`CHAT_RUN_INACTIVITY_TIMEOUT_SEC`）；只有后端注册表中确实存在待回答/待确认交互时，内部心跳才可续活该看门狗；周期 reaper 把超龄且静默的 running run 收成 failed；启动钩子 `recover_orphan_runs()` 清理重启遗留。

### 智能体主动询问用户

普通顶层对话会注册 `ask_user_question`。智能体只能在用户偏好、授权决定或会实质改变结果且无法通过上下文和工具查明的歧义上调用它，并应把同一决策点集中成一轮少量简洁问题。极速、计划执行、自动化和子智能体等无人值守/非顶层路径不注册该工具，避免后台任务无限等待界面输入。

模型可见契约与 deepseek-harness 一致：`questions[]` 包含 `id`、`question`、可选 `header`、由 `label/description` 组成的可选 `options`，以及 `multi_select`。选项没有模型可见的 ID 或 `recommended` 字段；推荐项放在首位，并在 label 末尾追加 `(Recommended)`。成功结果固定为紧凑的 `{"answers":[{"id":"scope","selected":["仅当前页面 (Recommended)"],"custom":"..."}]}`，其中 `selected` 保存原始选项 label。浏览器作答接口使用的私有 option ID 只是传输细节，不会进入模型工具 Schema 或工具结果。

工具调用后，当前 ReAct 工具协程原地挂起，不结束 ChatRun，也不新建“继续回答”轮次。前端收到 `user_question` 后用常驻问答 Composer 替换普通输入框，支持单选、多选、推荐项、自定义补充、逐题翻页、跳过与取消；答案通过带外接口提交。成功 POST、`user_question_resolved` SSE 和 pending 权威快照都只按精确 `request_id` 移除问答框，因此重复点击、并发标签页和迟到响应都由服务端“首个有效提交者获胜”的状态裁决。

刷新、断线或切回会话时，前端通过 pending 接口恢复后端注册表中的真实待回答项，并在侧栏标为“等待你的回答”。默认最长等待由 `HUMAN_INTERACTION_MAX_WAIT_SECONDS=7200` 控制；等待超时、用户取消或界面不可用会分别成为结构化工具错误 `ASK_TIMEOUT`、`ASK_CANCELLED` 或 `NO_PROVIDER`，系统策略要求智能体不得原样重复询问，并在合适时采用稳妥默认值继续。注册表目前与现有写确认机制一样是单进程内存态；服务重启会将原 ChatRun 收为失败，前端不会把旧问题误报为可恢复。

### 运行中追加、Steer 与快捷停止

普通对话正在生成时，输入框仍可接收下一条消息。发送后，消息先显示在输入框上方的待发送卡片中；用户可通过更多菜单编辑，也可删除。若不执行 **Steer**，当前回答结束后会自动把这条消息作为下一轮发送。

首页与会话页的输入框都会随多行内容自动增高，达到可视高度上限后再在框内滚动；`Shift+Enter` 可输入换行。

追加指令以数据库为准：系统先持久化，再用 Redis 做尽力而为的低延迟唤醒。同一会话按单调 `steer_seq` 排序，状态为 `accepted`、`claimed`、`applied`、`cancelled` 或 `superseded`；认领租约过期后可重新投递。`delivery_mode=steer` 会在下一安全 ReAct 边界注入，并以 `steer_applied` 事件确认；`delivery_mode=followUp` 在当前 run 完成后启动，`delivery_mode=nextRun` 不修改当前上下文，而是成为下一条独立 run。后两种交接会在同一事务中提交当前回答、排队用户消息、队列状态和下一条 `ChatRun`，刷新后可通过上面的状态接口对账。包含附件、技能、连接器、插件或子智能体的消息仍会等待当前回答结束后正常发送。按 `Esc` 会取消当前页面正在显示的会话 run。

### 工具执行权限档

输入框工具栏「项目」选择框旁边有一颗权限胶囊，三档，按用户保存在服务端（`users_shadow.metadata.tool_approval_mode`），读写接口是 `GET/PUT /v1/tool-approval`：

| 档位 | 存储值 | 效果 |
|---|---|---|
| 逐项确认（默认） | `ask` | 保持原有行为：写文件、动本机、跑命令前弹确认条等用户点头 |
| 替我批准 | `auto` | 普通写入 / 编辑直接放行；**删除类操作**（`DESTRUCTIVE_OPS`）与本地策略标了 `danger:<类别>` 的命令仍然停下来问 |
| 完全放开 | `full` | 所有工具确认一律不再询问 |

档位在 `core/llm/tool_permissions.py` 里统一声明（`APPROVAL_ASK` / `APPROVAL_AUTO` / `APPROVAL_FULL`），随对话上下文传到 `PermissionRuntime`，是所有工具确认闸的唯一判据；任何一档都只跳过"问"这一步，本地安全策略判定的硬拒绝仍然拒绝。"这次危不危险"由 `core/sandbox/local_policy.danger_categories()` 从裁决理由里的 `danger:` 前缀还原，不靠匹配提示文案。读到早期版本存下的 `standard` / `readonly` 时按最保守的 `ask` 兜底。

渠道机器人、定时任务这类无人值守入口走 `default_allow` 分支，不受该档位影响。

**桌面端沿用同一档，不再另存一份权限档。** 原来的「本机操作权限档」（严格 / 标准 / 放开，存本机 `local_grants.json`，接口 `/v1/local/approval-mode`）已整体删除；本机执行策略改由 `core/services/local_grant_service.policy_for_gate(approval_mode)` 按本档位翻译：`full` 全部放行，`ask` / `auto` 走用户配置的分类处置与内置默认。OS 文件沙箱约束同样按档位声明（`LOCAL_CONFINEMENT_BY_MODE`）：`ask` / `auto` 优先约束、缺执行器时降级告警，`full` 显式不约束，读不出本机安全配置时落到 `FAIL_CLOSED_MODE` 这个记号档、要求强制隔离。桌面端仍保留的「授权目录 / 危险命令分类处置」（`/v1/local/grants`、`/v1/local/policy`）管的是"本机哪些目录能动"，与档位是两件事，入口仍在胶囊底部。

### Agent 构建要点（core/llm/agent_factory.py）

`create_agent_executor()` 是所有模式（主对话、计划、批量、子智能体、自动化）共用的工厂：

- **MCP 工具**：经 catalog + 用户覆盖 + 请求上下文三层过滤后（见 [能力目录](catalog.md)），stable 服务复用进程级连接池（`core/llm/mcp_pool.py`），per-request 服务（如 `retrieve_dataset_content` 需带每请求 HTTP header）每次新建；用户自助添加的私有 MCP 按 owner 现查合入。
- **技能**：经 `core/agent_skills/loader.py` 注册为 AgentScope Agent Skills，并放行 `view_text_file` 读取 SKILL.md（详见 [技能系统](agent-skills.md)）。
- **文件/沙箱工具**：`bash`、`sandbox_put_artifact`、`sandbox_get_artifact` 无条件注册；Read/Edit/Write/Glob/Grep/Delete/Move/mkdir + MySpace 工具受 `CODE_CAPABILITY_ENABLED` 门控，共享同一个 `ReadStateTracker` 维持「先 Read 才能 Edit」不变量。
- **中间件**（洋葱模型，`core/llm/middlewares.py`）：`DynamicModelMiddleware`（按 chat_mode 切模型，见 [模型接入](model-providers.md)）、`FileContextMiddleware`（注入上传/历史文件上下文）、`SteerMiddleware`（工具结果之后、下一轮推理之前注入追加指令）、`WorkspacePinHintMiddleware`、`FinishPinGuardMiddleware`。
- **上下文压缩**：`CompactingAgent` 把轮前、轮内和轮末三个触发时机接入同一个持久化 checkpoint 引擎，并共用 Codex 风格交接提示词；`ContextConfig` 只保留工具结果限长和 AgentScope 溢出兜底，兜底也复用同一提示词。若兜底的结构化调用仍失败，模型适配层会返回 L3 占位摘要，避免当前回答因压缩异常直接中断。
- **权限**：所有已注册工具 seed 原生 `PermissionRule(ALLOW)`，保留 AgentScope 内置工具的危险操作检查（不使用一刀切 BYPASS）。
- **迭代上限**：主智能体默认 `max_iters=50`，隔离子智能体默认 10。

## SSE 事件类型与负载

`orchestration/streaming.py::StreamingAgent` 把 AgentScope 2.0 `reply_stream` 的细粒度事件归并为内部事件；工具参数小分片按 256 字符或 50ms 合批，兼顾实时可见与 SSE 事件量。`workflow.py` 与 `chats.py::_stream_sse_response` 再补充会话级字段后落到 wire 上。前端实际收到的事件：

| `type` | 含义 | 关键字段 |
|---|---|---|
| `thinking` | 思考过程（增量或阶段提示） | `delta` / `message` |
| `content` | 回答正文增量 | `event: "ai_message"`, `delta`, `chat_id` |
| `content_replace` | 本体评审修订了已流式展示的草稿时，原位替换最终答案 | `content`, `reason: "ontology_review"`, `chat_id` |
| `tool_call_start` | 开始构造一次工具调用；前端按稳定 ID 创建一张卡片 | `tool_name`, `tool_display_name`, `tool_id` |
| `tool_call_delta` | 工具参数 JSON 增量；前端在同一卡片中追加 | `tool_name`, `tool_id`, `arguments_delta` |
| `tool_call` | 工具参数已完整、即将执行 | `tool_name`, `tool_display_name`, `tool_args`, `tool_id`，调子智能体时附 `subagent_name` |
| `tool_result` | 工具调用结果 | `tool_name`, `result`, `tool_id`, `status`, `citations[]` |
| `steer_applied` | 运行中追加指令已注入 ReAct 上下文 | `steer_id`, `message`, `message_id`, `chat_id` |
| `queued_run_started` | 已原子提交的 `followUp` / `nextRun` 子运行开始，前端立即接力续播 | `run_id`, `message_id`, `user_message_id`, `message`, `queue_id`, `steer_id`, `delivery_mode` |
| `subagent_event` | 子智能体内部过程，挂在父 `call_subagent` 卡片下 | `parent_tool_id`, `sub_type`, `agent_name`，以及内部工具或内容字段 |
| `ontology_activation` / `ontology_gate` / `ontology_review` | 本体治理状态，不属于模型思考 | 工作流、门禁决策、委员会状态与结论 |
| `tool_pending` | 提供商没有暴露可解析参数增量时的等待兜底 | `reason` |
| `batch_confirm` | 批量计划生成完毕，等待用户确认（人审门） | `plan_id`, `total`, `preview`, `default_template`, `placeholder_keys` |
| `file_confirm` | 工具挂起等待用户确认「我的空间」写操作 | 确认上下文；用户带外 `POST /v1/chats/{chat_id}/file-confirm` 后工具原地续跑 |
| `user_question` | `ask_user_question` 已挂起并等待会话属主回答 | `request_id`, `questions[]`, `created_at`, `expires_at`, `chat_id` |
| `user_question_resolved` | 回答、取消或超时已由服务端裁决 | `request_id`, `outcome`, `chat_id` |
| `context_usage` | 最近一次主模型调用的上下文占用快照；供应商返回 usage 时总数为实测值 | `source`, `exact`, `prompt_tokens`, `completion_tokens`, `used_tokens`, `context_window`, `breakdown` |
| `compaction_notice` | 上一轮结束后已生成新的上下文压缩检查点 | `chat_id`, `context_compaction`（覆盖边界、摘要基线 token 数） |
| `meta` | 回合收尾元数据 | `route`, `citations[]`, `sources`, `artifacts`, `workspace_files`, `ontology_governance`, `warnings`, `is_markdown`, `message_id`, `usage`, `context_usage`, `compaction_pending` |
| `error` | 出错（已映射为用户友好中文文案） | `error`, `chat_id` |
| `heartbeat` | 心跳（事件级；另有 `: heartbeat` 注释行） | — |

流以 `data: [DONE]` 结束。示例帧：

```
data: {"type":"tool_call_start","tool_name":"internet_search","tool_display_name":"联网搜索","tool_id":"call_abc"}

data: {"type":"tool_call_delta","tool_name":"internet_search","arguments_delta":"{\"query\":\"北京 集成","tool_id":"call_abc"}

data: {"type":"tool_call_delta","tool_name":"internet_search","arguments_delta":"电路 产业\"}","tool_id":"call_abc"}

data: {"type":"tool_call","tool_name":"internet_search","tool_display_name":"联网搜索","tool_args":{"query":"北京 集成电路 产业"},"tool_id":"call_abc"}

data: {"type":"tool_result","tool_name":"internet_search","result":{...},"tool_id":"call_abc","citations":[{"id":"e1","title":"...","url":"...","snippet":"...","source_type":"internet","item_index":0}]}

data: {"type":"content","event":"ai_message","delta":"根据检索结果……","chat_id":"chat_x"}

data: {"type":"context_usage","source":"provider","exact":true,"prompt_tokens":1234,"completion_tokens":456,"used_tokens":1690,"context_window":128000,"breakdown":{...}}

data: {"type":"meta","route":"main","citations":[...],"usage":{"prompt_tokens":3700,"completion_tokens":900,"total_tokens":4600,"llm_call_count":3},"context_usage":{"source":"provider","exact":true,"prompt_tokens":1234,"completion_tokens":456,"used_tokens":1690,...},"message_id":"msg_..."}

data: [DONE]
```

`meta` 之后，`chat_run_executor.py` 持久化助手消息、回填 artifact，并起后台任务生成追问问题（`orchestration/followups.py`，结果写进消息 `extra_data.follow_up_questions`，前端经 `GET /v1/chats/{chat_id}/messages/{message_id}/followups` 拉取）。本体事件在前端汇总为独立的“领域本体治理”模块，不再写入或显示在“思考过程”中。模型草稿保持逐 token 流式展示；委员会仅在实际修订答案时发送一次 `content_replace`，前端原位替换正文，数据库只保存评审后的最终答案。`ontology_governance` 随助手消息持久化，刷新历史会话后仍可回显。

`usage` 是本轮所有模型调用的累计计费数据；上下文仪表使用的是
`context_usage`，即最近一次主模型调用的 `prompt_tokens + completion_tokens`，
不能把多次 ReAct 调用的累计账单除以上下文窗口。供应商返回 usage 时，
`source=provider` 的总数为权威实测；分类明细来自后端最终请求 manifest，并按
实测总量归一。供应商不返回 usage 时才降级为 `backend_estimate`。前端不会把仅供
展示的子智能体 `subSteps`、历史 thinking 或固定 system reserve 计入。未发送的
草稿和待发送文件尚未发生模型调用，只能作为明确标注的本地预估附加在实测基线上。
该快照随助手消息持久化，并可通过
`GET /v1/chats/{chat_id}/context-usage` 单独读取。

历史回放先识别两种思考协议：内联思考模型可能只输出
`reasoning</think>正文`，结构化 reasoning 字段则由后端归一化为
`<think>reasoning</think>正文`。前端把思考与工具调用放入统一过程区，并把全部可见正文合并为一个连续的 Markdown 块；历史渲染不再按正文字符位置切分。

## 引用系统（Citations · 证据锚点）

引用让回答里的每个事实可溯源到具体工具结果。编号权收归后端唯一真源——模型只**复制**编号、不做任何计算，链路分四段：

1. **发号回注（后端中间件）**：`core/llm/middlewares.py::CitationAnchorMiddleware` 挂在 AgentScope 2.0 的 `on_acting` 钩子上，在工具结果回给模型前调用 `orchestration/citation_anchor.py` 完成 提取 → 发号 → 回注：为每条可引用条目分配会话内单调唯一的锚点 id（`e1`、`e2`、…，跨工具、跨调用、跨轮不重复，新一轮从该会话历史消息的最大锚点续号），并把 `"cite_id": "e7"` 就地写进结果 JSON（纯文本结果在文末追加 `[cite_id: e7]` 行）。**发号器绑在 agent 实例上**（`attach_allocator()` / `resolve_allocator()`）——编排层与中间件由此共享同一个计数器；ContextVar 只作子智能体链路的兜底，因为 `astream_chat_workflow` 是 async generator，其上下文与 agent 实际执行所在的 task 并不互通。提取按四层降级：工具自声明 `__citations__` → 工具规格注册表（`TOOL_SPECS` 配置，列表路径 + 中英字段别名）→ 通用启发式（唯一字典数组字段）→ 整份结果 1 个锚点；操作型工具（写文件、pin 等，`SKIP_TOOLS`）直接放行。任何异常原样放行、绝不阻断对话。
2. **提示词约定**：系统提示词（`prompts/prompt_text/default/system/40_format.system.md` 的兜底版本，运行时以 DB 激活版本为准）只需一条与工具数量无关的通用规则：把结果里标注的 `cite_id` 原样复制进 `[锚文本](cite:e7)`（或句末 `[来源](cite:e7)`），禁止自行编号。
3. **编排层消费**：每个 `tool_result` 事件经 `collect_citation_dicts()` 按 `tool_id` 从发号器注册表精确取 `CitationItem`（`id` / `tool_name` / `tool_id` / `title` / `url` / `snippet` / `source_type` / `item_index`）；发号器缺位（旧对话回放等）时回退 `orchestration/citations.py` 的旧偏移提取。`source_type` 取值：CE 内置 `internet`、`knowledge_base`、`database`；`industry_news`、`ai_news`、`chain_info`、`company_profile` 等行业引用类型由商业版 EE 扩展。
4. **前端渲染**：citations 随 `tool_result` 与 `meta` 事件下发并随消息持久化（落库的就是注号后的结果，回放/分享与生成时编号一致）。`components/citation/CitationMarkdownBlock.tsx` 并行识别三种标记——`[锚文本](cite:eN)`（渲染为带悬浮出处卡片的文字链接）、`[[eN]]`（obsidian 双链容错）、旧格式 `[ref:工具名-序号]`（历史消息，渲染为角标）；工具卡片条目上同步显示 `cite_id` 小徽章（`jx-tr-citeTag`），没有专属渲染器的工具由通用列表渲染器兜底成标准卡片。

**工具开发约定**：需要精确控制引用粒度的工具（自研或 MCP），在返回 JSON 里带 `__citations__` 字段——`[{"title": "...", "url": "...", "snippet": "...", "source_type": "..."}, …]`，条目顺序与结果正文对应；中间件优先采用并就地注入 `cite_id`。未自声明的工具按注册表配置或启发式提取，最差整份结果 1 个锚点——**任何工具默认可引用**。详见《[MCP 工具](mcp-tools.md)》的引用声明一节。

## 计划模式（Plan Mode）

计划模式把复杂任务拆成「生成计划 → 用户确认/编辑 → 逐步执行」两阶段，实现在 `orchestration/subagents/plan_mode.py`：

- **生成**（`astream_generate_plan` / `POST /v1/plans/generate`）：以 `disable_tools=True` 的"裸模型"产出结构化 JSON 计划。系统提示词解析顺序：版本池 `plan_mode` 激活版本 → 旧版 `system/90_plan_mode` 分段 → 文件兜底 `prompts/prompt_text/plan_mode/plan_mode.system.md` → 硬编码最小提示。
- **执行**（`astream_execute_plan` / `POST /v1/plans/{plan_id}/execute`）：每个步骤独立建 agent 顺序执行，支持步骤级 MCP/技能/子智能体绑定、取消（`is_run_cancelled` 轮询）；执行同样走 ChatRun + Redis Stream，可断线续播。
- **前端呈现与标题**：手动计划模式的预览和执行进度统一显示在对话内的计划卡片，不再重复显示输入框上方的紧凑计划条（该计划条只服务普通对话中的模型 `update_plan`）；计划预览生成后即触发会话标题自动摘要，摘要完成前由首条任务生成临时标题。
- **历史与当前模式解耦**：会话的 `planChat` 标记和 `plan_snapshot` 仅用于保留侧边栏类型及历史计划卡片；输入框是否继续走计划模式由独立的逐会话状态控制。用户关闭计划模式后仍能查看已有计划和报告，但后续消息（例如基于报告生成 PPT）按普通对话执行，刷新或重新进入会话也不会被历史计划自动重新开启。
- **模型角色**：计划模式优先解析 `plan_agent` 角色，未配置降级 `main_agent`（`agent_factory.py` `_mode_role` 分支）。
- 无人值守模式（计划执行 / 自动化）会从工具集中摘除 `batch_runner`，因为 `batch_plan` 的确认弹窗在该场景无 UI 可确认（`workflow.py::_resolve_batch_runner_visibility`）。

## 子智能体（Sub-agents）

普通主对话的 harness 始终提供三个平台默认子智能体。它们不是 `UserAgent` 数据行，使用保留 ID，不能被同名自建 Agent 覆盖：

| ID | 角色 | 对话上下文 | 项目工作区 | 能力边界 |
|---|---|---|---|---|
| `builtin.explorer` | 探索员 | 独立简报，不继承主对话 | 共享、只读 | 无 Bash；只取父级已启用的检索型 MCP 交集 |
| `builtin.worker` | 执行员 | 继承主对话完整历史 | 共享、可写 | 继承父级本轮技能、MCP 与 KB，不新增授权 |
| `builtin.reviewer` | 审查员 | 独立简报，不继承执行历史 | 共享、只读 | 无 Bash；独立核验，返回 `pass / revise / escalate` |

这三个角色会直接显示在用户侧「子智能体」页面并标记为「内置」，所有用户初始默认开启。用户可逐个启停；停用状态保存在该用户的 `users_shadow.metadata.disabled_builtin_subagent_ids` 中，因此可跨浏览器保持。已停用角色仍留在子智能体列表和 `@` 候选中，并标记为「未启用，仅本轮调用」；它不会进入主智能体的初始上下文或自主路由表，只有用户通过 `@` 或严格的自然语言调用明确指定时才临时加入本轮。角色提示词在详情页只读展示，不再作为 Config 提示词管理的可编辑标签页。详情页的「能力策略」会显示这些角色跟随主智能体在运行时动态加载能力，并明确探索员、审查员的只读收窄规则，不再把动态继承误显示为「未绑定」。

上下文和工作区是两个独立维度：探索员、审查员能检查当前文件，但不会被主对话或执行过程锚定；执行员则需要完整继承用户约束和既有决定。主智能体的动态路由表只展示本轮最终启用且角色策略允许下放的技能、MCP、知识库和基础工具；主智能体已关闭、未授权或被运行时过滤的能力既不会展示，也不会传给默认子智能体。三个角色都不能继续调用子智能体。默认列表不含 planner：普通主对话已用 `update_plan` 维护计划，避免形成“计划里的计划”。角色提示词在运行时仍来自版本池 `subagents` kind 的 `explorer / worker / reviewer` 三个独立 part，文件系统目录 `prompts/prompt_text/subagents/` 只负责种子和故障回退；该 kind 保留在后端与迁移快照中，以兼容既有环境。

除此之外，用户自建子智能体（`api/routes/v1/agents.py`，DB 表 `UserAgent`）可绑定独立的系统提示词、MCP / 技能 / 插件 / KB 集合与模型参数（provider / temperature / max_tokens / max_iters）。创建或编辑时，资源选择器支持以下来源：

- 已安装的技能与插件；
- 技能市场和插件市场，安装完成后自动绑定到当前子智能体；需要凭据的市场资源仍先走原有凭据配置与安装权限校验；
- 当前用户未启用、但管理员仍允许使用的 MCP。该绑定只对当前子智能体生效，不会同步开启主智能体的个人能力开关；管理员全局停用的 MCP 仍不可绑定。

用户自建子智能体有四种触达方式；平台默认角色通过主智能体自主编排或自然语言显式委派触达。编排归属取决于用户是否明确指定了目标：

聊天输入框提供两个键盘启动器。输入 `@` 会先显示文件、智能体、计划模式、批量执行、工作流模式和
自主循环等当前可用快捷入口；继续输入可直接筛选智能体，选择「智能体」则进入完整智能体列表。
输入 `/` 会按插件、技能分组显示命令及说明；对话模式统一通过 `@` 选择。两个面板都支持方向键选择、
Enter / Tab 确认和 Escape 返回或关闭。个人在能力中心关闭的技能、插件和智能体仍会出现在候选中。
智能体标记为「未启用，仅本轮调用」；技能与插件标记为「未启用，调用后本会话保持加载」。这些操作
都不会改回能力中心开关，但技能或插件首次显式加载成功后，会在同一会话后续回合继续装配。管理员全局
停用、未安装、依赖未就绪或当前用户无权访问的能力不会出现在候选中，也不能通过伪造 ID 绕过。

输入框左下角的 `+` 菜单还可直接选择当前用户可访问的智能体、技能、连接器和插件，包括个人已关闭项。
选择连接器后，前端会显示
一枚 `MCP` 芯片，并显式激活该连接器；发送后，所选连接器会作为徽标保留在会话记录中。首次装配成功
后，该连接器也会在同一会话的后续回合保持展开。
显式选择连接器属于强制调用：模型首轮只可从该连接器暴露的工具中选择，并且必须完成至少一次真实
工具调用后才能继续回答。若连接器无法连接、没有可用工具，或模型供应商不遵守强制调用约束，本轮会
明确报错并停止，不会静默跳过连接器后直接生成答案。
通过 `/` 显式选择技能后，模型必须先读取该技能自己的 `SKILL.md`，读取其他技能不能替代；加载失败时
本轮会明确停止。首次加载成功后，该技能在同一会话后续回合继续出现在技能清单中。
显式选择插件同样属于强制执行：模型必须先读取该插件自己的一个技能文件，或真实调用该插件暴露的
一个 MCP 工具，才可以继续完成回答；其他技能或连接器调用不能替代这项证据。插件没有可执行能力、
能力未能装配，或模型绕过强制约束时，本轮会明确失败。加载成功后，后端以插件安装实例 ID 记录会话
激活态；后续回合会在默认能力筛选前恢复并重新校验它，因此工具名与 MCP 前缀保持稳定。插件被卸载、
管理员全局停用、依赖失效或归属变化时不会被恢复。

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
  该回合仍是 `main` 路由，`call_subagent` 及内部工具各自保留真实审计日志。个人停用的目标仍可通过这条
  显式路径临时调用；名称重复、管理员停用或无权访问的目标、任务为空，
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
| 统一上下文检查点 | `core/services/compaction_service.py::run_compaction()`；轮前、轮内、轮末共用同一触发比例、交接提示词、替代历史结构和持久化 checkpoint | 上下文达到模型窗口的配置比例时 |
| 确定性溢出保护 | `ContextConfig.tool_result_limit` 先限制单条工具结果；统一压缩失败或持久化关闭时，AgentScope 兜底复用同一交接提示词压缩当前内存上下文 | 单条工具结果过大，或统一压缩暂时不可用时 |

压缩检查点是内部 `system` 消息，不会隐藏或删除任何用户可见的对话记录。检查点同时
保存压缩后最终 system prompt、工具 schema、replacement history 与 provider
framing 的后端估算。当前回合结束后若后台压缩已启动，`meta.compaction_pending`
会让前端在摘要超时窗口内有界轮询 `GET /v1/chats/{chat_id}/context-usage`；
新检查点提交后仪表立即切换为 `compaction_estimate`，不必等下一轮或刷新页面。
下一次主模型调用结束后，新的上游 usage 会再次替换该估算。消息列表响应和下一轮的
`compaction_notice` 仍会返回同一检查点边界，保证刷新、断线续播和跨标签页恢复一致。

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
| 标题摘要 / 上下文压缩 / 窗口保护 | `src/backend/core/llm/summarizer.py`、`compaction.py`、`core/services/compaction_service.py`、`context_manager.py` |
| 超长结果 offload | `src/backend/core/llm/offloader.py` |
| 会话分享 | `src/backend/api/routes/v1/chat_shares.py` |
| 追问生成 | `src/backend/orchestration/followups.py` |
| 前端流式解析 / 追加消息 | `src/frontend/src/hooks/chatStream.ts`，`useStreaming.ts`，`components/chat/QueuedMessageCard.tsx` |
