# Personal Agent Super-agent 重构设计：全双工、多信道与 Watchdog

状态：草案，待评审（第二版：已对照 core 代码核对，见 §17）。本文只描述设计，不包含代码改动。本文是重构的唯一设计文档；[ROUTER_DESIGN.md](ROUTER_DESIGN.md) 中 Router 的 Trigger / Turn / 批处理内核作为本文 §7 的实现细节保留，引用关系见 §16。

参照：
- 《Exploring Super-agent Design》第 4.1 节（全双工交互）、第 4.2 节（多信道交互）、第 6 节（Watchdog）。
- DeepSeek 的 `dsh-superagent-prototype`（commit `6337fc8`，下文简称 DSH 原型）。它的 README 说明，文中部分设计没有在原型中实现，**Watchdog 就是其中之一**。

## 0. 结论

Personal Agent 已经有 Router（front turn）+ Worker（后台任务）的双层结构，Router 一侧的委派也是异步的：`pa_task_create` 立即返回，任务结果作为事件回到主会话。问题不在于缺少某个功能，而在于**执行内核是自建的**：

- `turn_worker` + `front.run_turn`（Router 一轮）和 `tasks/worker` + `tasks/runtime`（Worker 一次 run），各自实现了租约、重放、流式记录、steering 和失败兜底；
- 6 个以上的后台轮询循环各自做重试；
- 这些都与 core 已有的聊天执行内核平行。core 已经实现的后台命令完成唤醒、steer 队列、运行看门狗，personal_agent 一个都没有用上。

结果是：
1. **Worker 是阻塞式 ReAct**。`pa_bash` 用 `run_to_completion`；`pa_hugagent run.status` 最长阻塞 600 秒。
2. **Worker 每次 run 都重新拼 prompt**，没有持久的会话历史。
3. **对外输出有多个出口**：`notify` 和 `approval_notice` 绕过了 Router。各信道没有差异化规则，也没有硬上限。
4. **没有外部控制回路**，也没有进展语义：只要租约在续，任务就被视为健康。

本设计做四件事：

| # | 改动 | 解决 |
|---|---|---|
| A | **执行内核重做**。Worker 换成普通的 core ChatSession，由 `ChatSequencer` / `ChatRun` 驱动，需要三处 core 小改动。Router 保留自建的 Trigger / Turn 内核，带批处理和推理中吸收（§7.1、§17）。personal_agent 只保留领域逻辑：任务、报告、路由和信道 | 4.1 全双工，并删除平行内核 |
| B | **领域拆分**。`PersonalWorkItem` 拆成 Task（工单）、Assignment（一次指派）、Worker Session（持久历史）三部分；Worker 用结构化 Report 回报 | 4.1、4.2 |
| C | **信道层**：回复用途、按信道的硬上限、唯一的输出出口、有序 Outbox | 4.2 |
| D | **Watchdog**：独立进程，由定时消息驱动；先做确定性探测，再做模型判断；有在线可改的提示词与规则版本 | 第 6 节 |

## 1. DSH 原型中值得借鉴的做法

| 做法 | DSH 原型 | 对应本文 |
|---|---|---|
| 收件箱的三种投递模式 | `queue`（下一轮）/ `steer`（当前轮下一步）/ `inject`（写入历史但不唤醒），见 `app/sessions.ts` 的 `prompt` | §6.2 |
| 长命令自动转后台 | worker 预设：前台 shell 超过 5 秒自动转为 job；job 结束后用结果唤醒 Session；提示词禁止 sleep、轮询或等待 job | §6.3 |
| 轮次结束不等于工作结束 | 有后台 job 时继续占用槽位（`untilQuiet`），后台最多 30 分钟，超时 kill 后报告 waiting | §6.3 |
| 持久的 Worker Session | 每个工单一个 `worker-<ticket>` Session，历史跨多次指派保留 | §5、§8 |
| Router 只路由 | orchestrator 不读代码、不规划、不改写请求，路由备注 ≤30 词；worker 自己读原始消息 | §7 |
| Subscription 扇出 | 工单所在线程上的后续人类消息直达 worker，Router 只收到 follow_up 通知；worker 用 `ack` 接下，或用 `handback` 交回 | §8.3 |
| 结构化 Report | progress / waiting / ready / blocked / failed / dropped。progress 不唤醒 Router；结构化的 `human_action` 问题立即转给 Router | §8.2 |
| 回复用途 | acknowledgement / answer / result / attention，由工具强制检查前置条件 | §10.2 |
| IM 100 词硬上限 | 在工具 schema 中用 `Intl.Segmenter('zh')` 计数，超出直接拒绝 | §10.3 |
| Effects 队列 | 先落库再执行；按 kind/key 去重；指数退避；`Later` 延后；同一 kind 连续失败 3 次上报健康状态 | §9 |
| 有序 Outbox | 同一工单发往同一会话的消息保持顺序，失败 3 次后才放行后面的；记录回执和丢弃原因 | §10.4 |
| 版本化提示词与策略 | `Versions`；主 Session 可以用 `orchestrator_prompts_save` 在线修改；`policyChanged` 通知受影响的活跃工单 | §11.5 |
| 工单状态由领域决定 | Ticket 状态不跟随 Session 状态；Assignment 独立存在 | §5 |

不照搬的部分：Policy / Mandate / 准入表这套多人授权体系。Personal Agent 只有一个 owner，只保留"群聊中谁的话算请求"这一条（§10.5）。

## 2. 现状诊断

| # | 位置 | 问题 | 对照准则 |
|---|---|---|---|
| G1 | [tools/tools.py:68](tools/tools.py) `pa_bash` | 调用 `run_to_completion`，长命令期间 Worker 无法响应新输入 | 4.1 |
| G2 | [sessions/session_tool.py](sessions/session_tool.py) `MAX_WAIT = 600` | `run.status wait_seconds` 在工具内轮询，最长阻塞 10 分钟 | 4.1 |
| G3 | [tasks/runtime.py](tasks/runtime.py) `_task_inputs` | 每次 run 都重新拼 prompt：goal + 最多 30 条更新 + checkpoint JSON + 最近 12 条消息。跨 run 的推理和工具结果全部丢失，私有会话只用来展示 | 4.1 |
| G4 | [core/chat/steer_hook.py:21](../core/chat/steer_hook.py) `before_tool` | 新输入一到就拒绝工具调用，这是"打断"而不是并行 | 4.1 |
| G5 | [conversation/turn_worker.py:39](conversation/turn_worker.py) | Router 同一 agent 一次只跑一轮，一行 inbox 对应一轮；推理期间到达的输入不会被吸收（ROUTER_DESIGN P1–P9） | 4.1 |
| G6 | [conversation/front.py](conversation/front.py) `announce`、[sessions/session_watch.py](sessions/session_watch.py) | 每个任务结果、每次 session 结束都会唤醒一整轮 Router，没有"只记录、不唤醒"的投递方式 | 4.1、4.2 |
| G7 | [workspace/prompts/front_rules.system.md](workspace/prompts/front_rules.system.md) | 要求 Router 写"self-contained goal"，Router 的理解偏差直接变成任务目标 | 4.2（双层隔离） |
| G8 | [tasks/worker.py:65](tasks/worker.py) `notify`、`im_actions.approval_notice / question_notice`、`pa_deliver` | Worker 直接写主会话或 IM，绕过 Router 和信道适配 | 4.2 |
| G9 | `front.finish` → `mark_for_delivery` | 同一段文本同时写网页和 IM；`front_event` 要求交付物"include all of it"；超过 8000 字的结果原样发出 | 4.2 |
| G10 | `channel_bridge.SUPPORTED` | 信道只建模成 IM 适配器；邮件只是 Worker 的一个工具，不是输出信道；没有异步高密度信道的规范 | 4.2 |
| G11 | `channel_bridge.admit_inbound` | 群聊消息只加说话人前缀；personal_agent 内没有看到发送者准入校验（**待确认** integrations 层是否已过滤） | 4.2、安全 |
| G12 | [background.py](background.py) | 8 个循环和 Worker 在同一进程；`every()` 吞掉异常只打 warning；没有存活心跳 | 6 |
| G13 | `tasks/worker.recover`、`runtime.runaway` | 只处理租约过期和"文本重复"；工具调用循环和长时间没有实质进展都检测不到 | 6 |
| G14 | `_record_failure`、`pause` | 故障的终点是"通知用户点重试"，没有自动诊断，也没有持续复查 | 6 |
| G15 | `workspace/prompts/*.md` | 提示词是仓库文件，不能在线修改，也不能回滚 | 6 |
| G16 | `PersonalWorkItem` | 一张表同时承担工单、本次执行（`run_id` / `owner` / `lease_until`）和执行器状态，重试、恢复、电脑等待都在上面叠特判 | 结构 |

## 3. core 中已有、可直接复用的能力

这是本设计成立的前提：大部分全双工能力 core 已经有了，只是 personal_agent 没有用。

| 能力 | 位置 | 现状 |
|---|---|---|
| 托管进程：先 yield，再后台运行 | `SandboxProvider.start_process(yield_time_ms)` / `write_stdin`（[core/sandbox/protocol.py](../core/sandbox/protocol.py)） | 普通聊天的 `Bash` 已经在用（[core/llm/tools/sandbox_tool.py](../core/llm/tools/sandbox_tool.py)） |
| 后台命令管理与完成通知 | `core/sandbox/commands/`：`CommandRuntime.start/adopt`、输出收集器，返回"Completion will notify this chat. Do independent work; do not poll" | 普通聊天已在用 |
| 完成事实注入运行中的轮次 | `completion_hook.inject_completion`（TRANSFORM_CONTEXT，在 reasoning 边界注入） | 同上 |
| 空闲会话被完成事件唤醒 | `commands/delivery.prepare` → `ChatSequencer.accept_main_run(wake_reason="command_finish")` | 同上 |
| 有命令未完成时保持运行 | `completion_hook.pending_completion` → `continue_run` | 同上 |
| 会话级串行写入与运行 | `ChatSequencer`（`accept_main_run` / `get_active_run` / `release_writer`）、`ChatRun` | 普通聊天 |
| 输入投递模式 | `steer_queue`：`steer` / `follow_up` / `next_run` | 普通聊天。**注意**：它不等于 DSH 的 inbox，不能直接拿来做批处理，见 §17 V2–V4 |
| 多个事件合并成一次唤醒 | `commands/delivery.prepare(rows)`：同一 chat 的多条命令完成通知合并成一条隐藏输入，只开一个 run | 普通聊天。这是 core 里现成的"空闲时批处理"写法：Worker 的 Executor 照这个模式合并唤醒；Router 在自己的调度器里做同样的事 |
| 运行看门狗（基础设施层） | [core/runs/watchdog.py](../core/runs/watchdog.py)：流无活动超时、回收过期 run | 普通聊天 |
| 上下文压缩 | `core/chat/compaction/` | 普通聊天 |
| 任务私有会话 | `task_threads.sync_thread` 已经为每个任务建了 `ChatSession`（`pa_chat_task_*`），goal、updates 和 final 都镜像在里面 | 只用于展示 |

## 4. 目标架构

```
             ┌──────────── 主系统进程（API / worker） ─────────────────────────────┐
 Sources ──▶ │ intake ─▶ Trigger ─▶ Lane(Router ChatSession) ── core chat runtime │
 (web / IM / │                │ follow-up 直达                                       │
  邮件 / 定时 │                ▼                                                     │
  / 心跳)     │        Task ─ Assignment ─▶ Worker ChatSession ── core chat runtime │
             │                ▲  Report / Input            │ Bash 后台 job        │
             │   Effects 队列（launch / deliver / relay / notice / reply）        │
             │                │                                                     │
             │   Outbox ─▶ ChannelRenderer ─▶ IM / web / 邮件 …                    │
             │   HealthBoard（各组件单写者）、Telemetry（步数 / 工具 / 进展）       │
             └────────────────▲───────────────────────────────────────────────────┘
                              │ 只读视图 + 受限动作 API（也走 Effects）
             ┌────────────────┴───────────┐
             │ Watchdog 进程（独立部署）  │ ◀── 定时消息（如每 5 分钟）
             │ 一个完整的 agent session   │ ◀── 维护者用自然语言提问
             └────────────────────────────┘
```

原则（沿用 DSH 原型的依赖规则）：
- **每种状态只有一个写入者**：Trigger、Task、Assignment 由 `personal_agent/core` 的 Engine 写；Outbox 回执只由 Outbox 写；健康条目只由声明它的组件写。
- **先落库再执行**：路由决定、指派、投递、回复都先写 Effects 行，提交后再执行。
- **请求与事实分开命名**：请求用命令式（`stop_task`），可以被拒绝；事实用过去式（`assignment_started`）。

## 5. 领域模型

### 5.1 概念

| 概念 | 定义 | 来源 / 替代 |
|---|---|---|
| Source | 事件来源：web、IM 机器人、定时、心跳、session 结束、邮件（后续） | 现有 `admit` / `heartbeat.admit` / `schedules` / `session_watch` |
| Trigger | 一个不可变事件，带来源元数据（发送者、信道地址、附件） | `personal_message_inbox` 改造（同 ROUTER_DESIGN §2.1） |
| Lane | 一条会话车道，即 Router 的 ChatSession：主会话，或某个 IM 会话的 `channel_session` | 现有 chat_id |
| Task | 一项被接受的工作，有自己的状态：open → ended，可以 resume | `PersonalWorkItem` 中的工单部分 |
| Assignment | Task 到 Worker Session 的一次绑定：pending → running → ended | 新表，接手 `run_id` / `owner` / `lease_until` / `auto_resumes` |
| Worker Session | 每个 Task 一个持久的 ChatSession（复用 `pa_chat_task_*`） | 现有私有会话，从"只展示"升级为模型历史 |
| Input | 投递给 Task 的上下文：用户更新、follow-up、job 完成、Watchdog 纠偏、策略变化 | 现有 `PersonalMessageTask(relation="update")` + 新类型 |
| Report | Worker 对 Task 的结构化状态：`progress / waiting / ready / blocked / failed / dropped`，可以附带 `question` | 现有 `pa_finish(done/blocked/dropped)` + `pa_task_checkpoint` |
| Subscription | Task 与 Lane 或消息线程的关联：线程上的后续消息直达 Task | 现有 IM 引用直达（`im_actions.route`）的泛化 |
| Channel | Lane 的写入端，带 ChannelProfile | 现有 `channel_origin` |

### 5.2 Task 阶段（取代散落的 status 判断）

参照 DSH 原型的 `ticketStage`，按顺序匹配，第一条命中的生效：

| 阶段 | 条件 | 页面标签 |
|---|---|---|
| person / delivery | Input 投递失败，正在退避重试 | 输入未送达 |
| person / approval | 有待批准的动作 | 等你批准 |
| person / answer | 最新 Report 带 `question` | 等你回答 |
| person / blocked, failed | 最新 Report 为 blocked 或 failed，并且此后用户没有发过消息 | 受阻 · 失败 |
| working / queued | 没有 Assignment，或 Assignment 为 pending（在等槽位） | 排队中 |
| working / computer | 等待桌面资源 | 等电脑 |
| idle | 最新 Report 为 waiting，或 Assignment 已结束且没有后台工作 | 等待中 |
| working | 其他情况 | 进行中 |

`computer_wait`、`waiting_reason`、`auto_resumes`、`decided_during_run` 等现有特判字段都改为由这张表推导。

## 6. 执行层：Worker 跑在 core 聊天运行时上（Router 见 §7.1）

### 6.1 做法

- Worker 的一次激活，就是它的 ChatSession 上的一个 `ChatRun`：由 `ChatSequencer.accept_main_run` 接受，由 core 的 run 执行器运行。profile 仍然用 `personal_agent`，工具集、提示词和 turn_context 由 profile 提供。
- 删除 `tasks/worker.PersonalAgentWorker.run`、`tasks/runtime.execute_task`、`task_threads.Transcript`、`tasks/steering.py`：Worker 的流式记录、分段、重放、取消和恢复全部交给 core。（`turn_worker.py` 和 `front.py` 由 Router 内核 `router/` 取代，见 §7.1。）
- personal_agent 只在 run 的边界接入：
  - `on_run_finished(run)` → 根据最后一次 Report 结算 Task 和 Assignment；
  - `on_run_failed(run)` → 区分瞬时故障与其他故障（§6.5）。

**已核对（§17 V1）**：后台发起 run 的路径存在（`commands/delivery.prepare` → `accept_main_run` → `admission.start_run`，`session_messages` 由调用方传入），但主路径 `agent_runtime/prepare_main.py` 构建执行器时不传 profile（`agent_spec=None`），所以 **personal profile 目前跑不了 core run**。需要一处 core 改动：`context` 里带 `profile` / `profile_ref` 时，`prepare_main` 透传给 `create_agent_executor`。改动小，不影响普通聊天。

**本节只适用于 Worker。Router 不迁到 core ChatRun，理由与做法见 §7.1。**

### 6.2 投递模式

| 模式 | 语义 | core 现状（§17 V2–V4） | 需要的 core 改动 |
|---|---|---|---|
| `steer` | 当前 run 的下一个 reasoning 边界注入**全部**积压输入；没有 run 时合并成一次唤醒 | 默认 `replace_latest=True`，新 steer 会把未领取的旧 steer 标成 superseded；`claim_next` 每个边界只领一条；必须有活跃 run | `accept(replace_latest=False)` 由调用方传入；新增 `claim_batch(run_id, max_items, max_chars)`，`SteeringHook.poll` 可选用它；没有活跃 run 时由 personal 侧照 `commands/delivery.prepare` 的写法合并唤醒 |
| `queue` | 当前 run 结束后开新 run | `follow_up` 可用，但 run 结束时 `consume_next_handoff_in_session` 每个后继 run 只消费一条 | handoff 改为一次消费同一 chat 所有可合并的条目，或者 personal 侧不用 `follow_up`，改为结束后由 Executor 合并唤醒 |
| `inject` | 写入历史，不唤醒 | **没有**。`next_run` 会在 run 结束时被 handoff 消费并开出后继 run，也就是会唤醒 | 不进 steer 队列：直接写一条 ChatMessage（`extra.context_only`），由下一次激活的历史自然带上 |

`SteeringHook` 增加两个参数：
- `interrupt_tools: bool`：Router 和 Worker 都传 `False`。已经发出的工具调用照常执行，新输入在下一个 reasoning 边界进入（解决 G4）。
- `take_batch: bool`：为 `True` 时一次领取全部积压输入，渲染成一条注入。

默认值都保持现状，普通聊天的行为不变。

### 6.3 后台工作

- `pa_bash` 删除，Worker 改用 core 的 `Bash`、`write_stdin`、`list_processes`（工作目录仍是 `/workspace/work/<task_id>`）。命令超过 `yield_time_ms` 后转为后台，完成时由 core 唤醒该 Worker Session（解决 G1）。
- Worker profile 的 `yield_time_ms` 默认值从 60000 降到 **5000**，与 DSH 原型一致，让新输入最多 5 秒就能进入。
- `pa_hugagent run.status wait_seconds` 删除。远程 run 的结束由 `session_watch` 以 Input（`steer`）投递给**发起它的 Task 的 Worker**，而不是像现在这样先唤醒 Router（解决 G2、G6）。
- **后台预算**：run 结束时如果还有后台命令，Assignment 保持 running 并占用槽位；超过 `BACKGROUND_LIMIT = 30 min` 后 kill 这些命令，记录 waiting Report，并把被 kill 的命令列表注入下一次激活。
- **交给用户时**（ready、blocked 或带 question）：释放槽位；留给用户使用的进程（例如一个预览服务）可以继续运行，但结束时不再唤醒 Worker。

### 6.4 槽位

保留 `concurrency(agent.config)`，另外加一个**用户消息专用的额外槽位**（DSH 原型做法）：用户对已有任务的 follow-up 优先于新任务的启动，新工作不能挡住用户继续推进已有任务。

### 6.5 失败与恢复

| 情况 | 处理 |
|---|---|
| 瞬时故障（408 / 429 / 5xx、连接被拒 / 重置 / 超时） | 退避重试 3 次（15 s、30 s、60 s），重试时注入"继续"Input |
| 其他故障 | Report `failed`，Task 进入 `person / failed`；此后不再接收自动 Input（定时结果、session 结束），用户发来消息即解除 |
| 进程重启 | 运行中的 Assignment 结束，标为"host restarted"；新 Assignment 带一条 recovery note："先核对工作目录、已发送的邮件和已执行的电脑操作，再决定是否重复外部操作"。电脑操作和邮件发送的"结果未知"仍由 `tool_effects` 决定是否要用户核对 |
| 已投递但没有被模型读取的 Input | 新 Assignment 重新投递（以 `consumed_message_ids` 判断，现有机制保留） |

## 7. Router

### 7.1 批处理与吸收

**决定：Router 保留自己的 Turn 内核，不迁到 core ChatRun；Worker 迁（§6、§8）。** 采用 ROUTER_DESIGN §2–§6 的完整设计：Trigger、Turn 表、调度器、TriggerKind、Grants、FrontSteering、ReplyStream。

不迁 Router 的原因：core 的 ChatRun 模型是"一条用户消息 → 一个 run → 一条助手消息"，Router 的输入形态与它不匹配：

| Router 的需要 | core ChatRun 的现状 |
|---|---|
| 一轮覆盖多个已经存在的 Trigger 行（用户消息、system 事件），各自带 IM origin 和占位符 | `accept_main_run` 自己新建一条用户消息（[sequencer.py:124](../core/chat/sequencer.py)），每个 run 一条 |
| 每个 Trigger 按 kind 结算：任务链接、投递、home 投递、记忆、quiet | run 只有整体的成功或失败 |
| 权限随批次计算（Grants），工具按 Trigger 归属 | run 没有这个概念 |
| 按 agent 串行（memory 文件、任务操作） | 按 chat 串行 |
| 历史中事件按 kind 渲染（任务结果作为 user 段、心跳只显示当前一次、Dreaming 过滤） | `_load_history` 只认 core 自己的行格式 |

硬塞进去，要改的是每个普通聊天都在用的 admission 和 handoff 语义。Worker 则正好相反：一个任务一个会话，输入就是"新的一条消息"，和 core 的模型一致。

Router 复用 core 的部分：`create_agent_executor` / `StreamingAgent`（已在用）、`durable_run_binding`（已在用）、`SteeringHook`（加 §6.2 的两个参数）。回复分段用 ROUTER_DESIGN §5 的 `SegmentedRecord`，但任务 `Transcript` 随 Worker 迁到 core 后会删除，所以它只服务 Router，放在 `router/reply.py`，不再抽到 core。

Router 的 Trigger 也是 Worker 的输入来源：Router 用 `pa_route` accept 或 attach 时，Engine 把 Trigger 以 Input 形式交给 Executor（§9 `deliver`），不经过 Router 改写。

### 7.2 Router 职责收窄

| 现在 | 改为 |
|---|---|
| `pa_task_create(goal)`：Router 写 self-contained goal | `pa_route(items=[{kind: accept / attach / stop / ignore, trigger_refs, title, note≤30 词}])`。Task 记录**原始 Trigger 引用**，Worker 自己读原文和附件；`note` 只写路由背景或用户的明确约束 |
| `pa_task_update(task_id, instruction)` | 拆成两种：`attach`（把这条消息挂到某个任务，原文以 Input 形式投递），以及 Subscription 直达（§8.3）。只有用户**明确指向**某个任务时才允许 attach：引用了它的消息，或只有一个任务符合"刚才那个" |
| 每个任务结果都唤醒 Router | 只有 `ready`、`question`、`blocked` 和 `failed` 会以 `steer` 转给 Router；`progress` 用 `inject` |
| Router 读任务细节、替用户判断任务技术问题 | 禁止；需要细节时用 `pa_task_request_status` 请空闲的 Worker 报告 |

直接回答（闲聊、常识、查进度、记忆、定时）的能力保留。Personal Agent 不只是派单器，这一点与 DSH 原型不同。

## 8. Worker

### 8.1 持久历史

- 模型历史就是 Worker ChatSession 的历史，由 core 的上下文和压缩负责。删除 `_task_inputs` 里的拼装逻辑（解决 G3）。
- 第一次激活时，系统消息包含：原始 Trigger（全文和附件）、Router 的 note、相关任务摘要、时钟。之后的激活只追加新的 Input。
- 运行中由 Watchdog 读取的"进展摘要"由 Telemetry 生成（§11.2），不影响模型历史。

### 8.2 工具

| 工具 | 作用 | 替代 |
|---|---|---|
| `pa_ack(trigger_ref, text≤30 词)` | 在开始实际工作前确认具体对象和预期结果。由 Outbox 回复到原消息，按 Trigger 去重 | 现在 Router 写的"我去……"那一句 |
| `pa_report(status, summary, question?, reply_to?, outputs?)` | 结构化报告。`progress` 不唤醒 Router；`question` 必须是一个具体问题 | `pa_finish`、`pa_task_checkpoint` |
| `pa_handback(trigger_ref, reason)` | 这条消息不属于本任务，交回 Router 重新评估 | 新增 |
| `pa_deliver` | 保留，但改为产出一个 Outbox 条目（purpose = `result` 的附件），不再直接写主会话 | 解决 G8 |

### 8.3 Follow-up 直达（Subscription）

- Task 订阅它的来源线程：IM 引用链、网页任务卡片会话、它发起的 HugAgentOS 会话。
- 这些线程上**用户本人**的新消息以 `steer` 直接投递给 Worker，并给 Router 一条 `inject` 的 follow_up 通知。Router 对 follow_up 只能执行"停止"。
- Worker 决定：属于本任务就 `pa_ack` 并接着做；不属于就 `pa_handback`。
- 任务结束时，如果还有没读过、没 ack、没 handback 的 follow-up，就回到 Router 重新评估（DSH 原型的规则）。
- 已结束的任务保留订阅：用户在它的线程上发消息即 resume，复用同一个 Worker Session。

## 9. Effects：统一的副作用队列

新模块 `personal_agent/effects.py`，新表 `personal_effects`（`kind`、`key` 唯一、`payload`、`due_at`、`attempts`、`last_error`、`status`）。

| kind | 处理方 | 并发 | 重试 |
|---|---|---|---|
| `launch` | Executor：创建或恢复 Worker Session，accept run | 空闲槽位数 | 瞬时故障 5 s 起翻倍到 300 s，3 次；`Later` 表示等槽位 |
| `deliver` | Executor：把 Input 按 §6.2 的模式投递给 Worker | 4 | 2 s 起翻倍到 60 s |
| `relay` | Router：把 Report、问题或 follow_up 投递给 Router Lane | 1，按序 | 2 s 起翻倍到 60 s |
| `notice` | Router：inject 通知 | 1，按序 | 同上 |
| `reply` | Outbox：经渲染器发往信道 | 1，按会话排序 | 5 s 起翻倍到 300 s |
| `watchdog_action` | Watchdog 动作执行器 | 1 | 2 s 起翻倍到 60 s |

- 由一个调度循环处理所有 kind，取代 `background.py` 中 channel、session watch 等各自重试的循环。schedules、heartbeat、dreaming、file memory 这些"产生 Trigger 的"定时器保留，但只负责 enqueue。
- 同一 kind **连续失败 3 次**即在 HealthBoard 上报 error（§11.1）。

## 10. 信道

### 10.1 ChannelProfile

```python
@dataclass(frozen=True)
class ChannelProfile:
    kind: str            # web | im_direct | im_group | email | github(后续)
    density: str         # low | high
    reply_words: int | None   # im_*: 100；web / email: None
    speak_when: str      # "addressed" | "always"
    renderer: str        # markdown | lark_rich | dingtalk_md | plain | email_html
```

- `turn_context` 增加一块 `# Channel`（profile 和规则文字），放在动态部分，不进 system prompt，保证缓存前缀不变。
- 新增提示词片段 `front_channel_im.system.md` / `front_channel_group.system.md` / `front_channel_async.system.md`：
  - **IM**：只在需要用户决定或验收时说话；只问直接的问题；给简短结论；不复述过程。
  - **群聊**：只回应 @ 或 owner 的直接请求；不主动插话。
  - **邮件和文档**：结构化、高信息密度：背景 / 结论 / 细节 / 链接。

### 10.2 回复用途

Router 的最终文本不再"自动投递"，而是通过 `pa_reply(purpose, text, task_id?, reply_to?)` 产出：

| purpose | 前置条件 |
|---|---|
| `answer` | 回复一条用户提问（Router 的直接回答；或 Worker Report 带了 `reply_to`，许可只能用一次） |
| `ack` | 只能由 Worker 的 `pa_ack` 产生 |
| `result` | Task 最新 Report 为 `ready` 或 `dropped` 且需要告知用户 |
| `attention` | Task 当前有 question 或待批准动作，文本必须包含该问题 |
| `alert` | 心跳或定时检查发现需要用户处理的事 |

网页始终显示 Router 的完整文本；IM 等低密度信道只收到经过 purpose 校验和字数校验的文本。现有 `pa_stay_quiet` 等价于"不调用 `pa_reply`"，只在没有用户消息的批次中才允许。

### 10.3 硬上限

- 新建 `core/infra/text.py`，提供 `word_count(text)`：对中英文混排计数，Markdown 链接的文字和地址都算。仓库目前没有分词依赖：引入 jieba 需要评审依赖；不引入时可以退化为"CJK 每 1.5 字计 1 词 + 拉丁词按空白切分"的近似计数，并在测试中对照 DSH 原型的 `Intl.Segmenter` 结果校准。
- 在 `pa_reply` 和 `pa_ack` 的参数校验中执行：`im_*` 超过 100 词直接拒绝，错误信息提示"缩短，不要拆成多条"。提示词加上 DSH 原型那句：不允许用长复合句、分号串、列表或连发多条来规避。
- 长交付物：IM 只发结论加一个网页链接（任务卡片或文件）。删除 `LONG_RESULT` 原样投递的分支。

### 10.4 Outbox

- 保留 `channel_bridge` 的持久 outbox，改由 `reply` effect 驱动。
- 同一 Task 发往同一会话的消息保持顺序；某条失败 3 次后才放行后面的。
- 回执有三种结果：`sent / dropped(reason) / waiting`。被丢弃的消息不再重发。
- `notify`、`approval_notice`、`question_notice` 全部改为产出 `attention` 或 `result` 回复（解决 G8）。

### 10.5 群聊准入

- 只有 owner 本人（channel 绑定的身份）的消息可以产生 accept；其他人的消息只作为 Lane 上下文，不唤醒 Router（`inject`）。
- **已核对（§17 V6）**：`integrations/channels/inbound.py:190` 只判断群消息是否 @ 了机器人，不判断发送者。personal 的 `admit_inbound` 也只是加上说话人前缀。也就是说，**群里任何人 @ 机器人，都能以 owner 的身份启动任务**（使用 owner 的电脑、邮件和设备）。这是安全问题，不应等到重构第 5 阶段，建议单独先修。

## 11. Watchdog

### 11.1 前提：可观测性（在主系统内实现）

| 组件 | 内容 |
|---|---|
| HealthBoard | 表 `personal_runtime_health(name, owner, status, detail, beat_at)`。每个循环和每种 effect kind 都单独登记，只由它自己写入；循环每轮写 `beat_at` |
| Telemetry | 每个 Assignment 记录：步数、各工具调用次数、最近 20 步的工具名与参数摘要的哈希、`last_progress_at`（最近一次 progress Report、文件变更或新的 Outbox 条目）、token 用量、后台命令列表 |
| 只读视图 API | `/internal/personal/watch/*`：健康状态、积压的 effects、活跃 Assignment 的进展摘要、最近的失败，仅限 Watchdog 服务账号访问 |

### 11.2 进程与驱动

- 独立入口 `personal_agent/watchdog/main.py`，compose 中作为单独服务（`personal-watchdog`），与主系统共享数据库，但不共享进程。主系统挂掉时它仍能发现并告警。
- 由定时消息驱动：每 `WATCHDOG_TICK = 5 min` 向 Watchdog 自己的 ChatSession 投递一条 `[tick] <时间>`，它本身也是一个跑在 core 运行时上的 agent session。
- 维护者可以在管理后台打开这个会话，用自然语言提问（"昨晚 3 点那个任务为什么失败"）或下达指令（"把 IM 的字数上限调到 80"）。

### 11.3 每个 tick 的两层检查

**第一层：确定性探测**（每个 tick 都跑，不调用模型，由代码直接执行）：

| 探测 | 判定 | 自动动作 |
|---|---|---|
| 循环心跳 | `beat_at` 超过 3 个周期 | 告警；同一进程多个循环同时停止时判为进程卡死，触发重启（经部署平台的 API，或只告警） |
| effect 积压 | 某个 kind 的最老 due 行超过阈值，或连续失败 | 告警，并附上 `last_error` |
| 租约异常 | running 的 Assignment 租约过期却没有被回收 | 执行回收 |
| Outbox | 某条消息失败 3 次以上 | 交给模型层判断是否改道（例如改发网页通知） |
| 沙箱 | `computer_status = degraded` 持续超过阈值 | 执行一次 `ensure_user_workspace` 重建；仍失败则告警 |
| 外部依赖 | 模型供应商 429/5xx 比例、IM 平台错误比例 | 标记为"外部偶发故障"，持续复查，不升级给用户 |

**第二层：模型判断**（只针对候选 Assignment）。候选条件（满足任一）：
- 步数 > N，且 `last_progress_at` 早于 15 分钟前；
- 最近 20 步中同一工具调用的哈希重复 ≥ 4 次；
- 运行时长超过同类任务的 P95；
- Report 在 `progress` 中反复出现相同 summary。

对每个候选，Watchdog 读取进展摘要：goal / 原始 Trigger、最近 Report、工具调用序列摘要、最近 3 段模型文本。然后给出判断和动作：

| 判断 | 动作 |
|---|---|
| progressing | 不动作，提高下次检查的阈值 |
| looping | 给 Worker 投 `steer` 纠偏 Input："你在重复 X，换一种方法或报告 blocked" |
| drifting | 投 `steer` 纠偏 Input，附原始目标；第二次仍偏离就停止 Assignment，Report `blocked`，由 Router 问用户 |
| stuck_external | 不动作，记入复查清单，下个 tick 再看；持续超过 1 小时才升级 |
| stuck_internal | kill 后台命令或重启 Assignment（带 recovery note） |

### 11.4 动作边界

- 所有动作都通过 `watchdog_action` effect 执行，并写入审计表 `personal_watchdog_actions(tick, target, judgement, action, evidence, result)`。
- **不允许**的动作：批准外部操作、发送邮件、直接给用户发 IM（只能通过 Router 产出 `attention`）、修改用户的 workspace 文件。
- 每个 Assignment 每小时最多干预 3 次；超过后只告警，交给维护者处理。

### 11.5 在线修正：提示词与规则版本

- 新表 `personal_prompt_versions(part, revision, text, base_revision, author, created_at)`。`workspace/prompting.prompt_part` 先读当前版本，没有覆盖时再读仓库文件。仓库文件是 base，版本表里只存差异。
- 可调规则（字数上限、Watchdog 阈值、心跳间隔上下限、BACKGROUND_LIMIT）放进 `personal_runtime_rules`，也做版本化。
- Watchdog 和维护者通过工具 `pa_prompt_save` / `pa_rule_save` 修改，必须带 base revision（防止并发覆盖），可以回滚。
- 每个 run 记录它用的提示词版本（DSH 原型的 `promptVersion`），便于事后归因。
- 变更以 `inject` 形式通知 Router 和受影响的活跃 Worker。

## 12. 模块布局

```
personal_agent/
  core/                 纯领域，不做 I/O（参照 DSH 原型的 src/core）
    model.py            Trigger / Task / Assignment / Input / Report / Subscription 类型
    engine.py           唯一写入者：receive、route、attach、report、assignment_* 事实
    stage.py            Task 阶段表（§5.2）
  router/               kinds.py、triggers.py、scheduler.py、turn.py、steering.py、reply.py（ROUTER_DESIGN §7）、context.py（system_prompt / turn_context / history）、tools.py（pa_route / pa_reply …）
  worker/               executor.py（launch / deliver / slots / background budget）、tools.py（pa_ack / pa_report / pa_handback / pa_deliver）
  effects.py            §9
  channels/             profile.py、outbox.py、renderers/、bridge.py（入站 → intake）
  health.py             HealthBoard、Telemetry
  watchdog/             main.py、probes.py、judge.py、actions.py、tools.py、prompts/
  identity/ workspace/ computer/ sessions/ scheduling/ delivery/   保留，调用方式改为经由 Engine 和 Effects
```

**删除**：`conversation/turn_worker.py`（由 `router/scheduler.py` 取代）；`conversation/front.py`（拆入 `router/`）；`tasks/worker.py` 的执行循环；`tasks/runtime.execute_task` 和 `_task_inputs`；`tasks/task_threads.Transcript`；`tasks/steering.py`（改由 core 的 steer + Input 投递）；`background.py` 中的 channel 和 session watch 循环；`tools/tools.py` 中的 `pa_bash`、`pa_finish`、`pa_task_checkpoint`。

## 13. 数据迁移

完整版和 CE 两条 alembic 链各加一组迁移，与 `personalsteer01` / `ce_0029` 一样成对处理。

1. 新表：`personal_front_turns`（Router 的 Turn，ROUTER_DESIGN §2.2）、`personal_assignments`、`personal_reports`、`personal_effects`、`personal_runtime_health`、`personal_assignment_telemetry`、`personal_prompt_versions`、`personal_runtime_rules`、`personal_watchdog_actions`。每张表跟着用到它的阶段一起迁移，不一次建完。
2. `personal_message_inbox`：加 `kind`、`chat_id`、`turn_id`、`task_id`（Input 指向的任务）、`mode`，删除 `owner` 和 `lease_until`；回填方式同 ROUTER_DESIGN §8。
3. `PersonalWorkItem`：
   - `run_id / owner / lease_until / auto_resumes` 迁到最新一条 Assignment；
   - `status` 改为 `open / ended` + `ended_reason`；
   - 显示用的阶段由 §5.2 推导；
   - `checkpoint.final_answer / pending_question / progress` 转成 Report 行。
4. 状态处理：`running` 的任务在迁移时标为 interrupted，升级后由 §6.5 的重启规则恢复；`responding / routing` 的 inbox 行改回 `pending`。
5. 部署要求：先停 worker 再升级。降级时只恢复列，不恢复运行态。

## 14. 分阶段实施

每个阶段都可以单独合并，并且有明确的验收标准。

| 阶段 | 内容 | 验收 |
|---|---|---|
| 0 core 改动与验证 | §17 已经核对完的部分不再验证。只做 core 的三处小改动，默认值都保持现状：`prepare_main` 透传 profile（V1）；`SteerQueue.accept(replace_latest=False)` 加 `claim_batch`（V2、V3）；`SteeringHook` 的 `interrupt_tools` / `take_batch`。然后验证 `Bash` 在个人电脑沙箱里可用（`/workspace` 挂载、用户身份） | 普通聊天回归测试全部通过；一个 personal profile 的 Worker Session 用 Bash 跑 `sleep 30; echo done`，期间连发 3 条消息，在 5 秒内于同一个边界被一次性处理，命令结束后被唤醒 |
| 0.5 Router 内核 | ROUTER_DESIGN §11 的第 1、3、4 步：Trigger / Turn 表与调度器（行为不变，批大小 1）→ 空闲批处理 → 推理中吸收 | ROUTER_DESIGN §10 的用例表 |
| 1 可观测性 | HealthBoard、循环心跳、Telemetry（只记录） | 停掉一个循环后，健康视图在 3 个周期内显示 error |
| 2 Worker 上 core 运行时 | Assignment 表、executor（launch / deliver / slots）、Worker 持久历史、core Bash 替代 `pa_bash`、`pa_report` 替代 `pa_finish` | `test_personal_agent_*` 的任务类用例改写后通过；"长命令期间追加指令"场景 E2E 通过 |
| 3 Effects | 统一的副作用队列，迁移 channel 和 session watch 的重试 | 断网注入：reply 退避后成功，health 状态先报 error 再自动恢复 |
| 4 Router 职责收窄 | `pa_route`（取代 `pa_task_create` / `pa_task_update`）、follow-up 直达、Report 驱动的 relay / inject。Router 内核已在 0.5 完成 | Router 不再改写 goal 的提示词回归评测；follow-up 直达与 handback 的用例 |
| 5 信道 | ChannelProfile、`pa_reply` purposes、字数硬上限、有序 Outbox、群聊准入、notify 改道 | IM 回复全部 ≤100 词；Worker 不再直接写主会话（用测试断言没有直接写入） |
| 6 Watchdog v1 | 独立进程、确定性探测、自动修复、审计 | 故障注入：卡住的循环、积压的 effect、过期租约、沙箱降级，都能在 2 个 tick 内处理 |
| 7 Watchdog v2 | 模型判断、纠偏 steer、提示词与规则版本、维护者会话 | 构造的"工具循环"和"目标偏离"任务被识别并纠偏；误判率在评测集上可接受 |

第 2 阶段是风险最大的一步：它删除了 Worker 的整套自建执行代码。第 0 阶段的验证不通过时，退路是保留 personal worker 循环，但把 `execute_task` 的模型调用换成以 Worker ChatSession 历史为输入，同时让 `pa_bash` 接入 `CommandRuntime`。这条退路可以拿到 80% 的收益。

## 15. 风险与待定问题

1. **core 改动的影响面**：§17 列出的三处 core 改动都以参数形式加入、默认值不变，但 `SteerQueue` 和 `SteeringHook` 是普通聊天的核心路径，第 0 阶段必须先跑完普通聊天的回归测试再继续。
2. **`inject` 的实现**：core 里没有"只记录"的投递方式（`next_run` 会唤醒）。本文改为直接写 `context_only` 的 ChatMessage，不新增 steer 模式。
3. **个人电脑沙箱与 core Bash 的差异**：用户身份、`/workspace` 路径、禁止的桌面凭证访问（platform_rules 中的 scrot / Xauthority 禁令）需要在 Worker profile 的 Bash 包装中保留。
4. **memory 文件并发**：Worker 持久化后可能有多个 Worker 同时写 `memory/YYYY-MM-DD.md`。现有 etag 乐观锁需要覆盖到 Worker 侧。
5. **提示词缓存**：ChannelProfile 和 Task 列表都放在 turn_context，system prompt 保持不变。
6. **压缩与任务状态**：持久 Worker Session 被压缩后，原始 Trigger 和 Router note 必须作为固定上下文项保留（`truncation_policy=never`）。
7. **Watchdog 的权限**：它需要一个服务账号和最小权限的动作 API，不能复用用户身份。
8. **群聊准入现状**（G11）需要先确认。
9. **迁移期间的 IM 占位符**（`im_actions` 的 placeholder）：ack 由 Worker 产出后，占位符的编辑和撤回规则需要重新定义。建议在 Router accept 时编辑成"收到"，Worker ack 时再编辑成具体内容。

## 16. 与现有文档的关系

- [ROUTER_DESIGN.md](ROUTER_DESIGN.md)：作为本文 §7 Router 内核的实现细节**全部保留**，包括 Trigger、Turn 表、调度器、TriggerKind、Grants、FrontSteering、ReplyStream 和测试用例。有两处以本文为准：
  - `SegmentedRecord` 只给 Router 用，不再与任务 `Transcript` 合并，因为 `Transcript` 会随 Worker 迁到 core 而删除；
  - `pa_task_create` / `pa_task_update` 上的 `from_messages`，改为 `pa_route` 的 `trigger_refs`。
- [README.md](README.md)：第 4 阶段合并后，按新模块布局更新 Responsibilities、Task steering 和 Task transcript ordering 三节。

## 17. 核对记录（第二版新增）

第一版有几处假设直接依赖 core 现有的能力，这一版对照代码逐条核对过。

| # | 假设 | 结论 | 依据 |
|---|---|---|---|
| V1 | core run 执行器能跑 personal profile 的后台发起 run | **部分成立**。后台发起的路径存在，但主路径不传 profile | `core/sandbox/commands/delivery.py:65` `prepare` → `accept_main_run` → `notifications.launch` → `admission.start_run`（`session_messages` 由调用方提供）；`core/agent_runtime/prepare_main.py:107` 中 `agent_spec=None`，没有 profile 参数 |
| V2 | core steer 可以把积压的输入一次注入 | **不成立**。默认 `replace_latest=True`，后到的 steer 会把未领取的旧 steer 标成 superseded；`claim_next` 每次只领一条 | `core/chat/steer_queue.py:222`、`:279`；`core/chat/steer_hook.py:15` `poll` 只取一条 |
| V3 | 空闲时 steer 会开新 run | **不成立**。`accept` 要求有活跃的 target run，否则抛出 `SteerQueueConflict` | `core/chat/steer_queue.py:178` |
| V4 | `next_run` 可以当作"只记录、不唤醒" | **不成立**。run 结束时 `consume_next_handoff_in_session` 会消费 `next_run`，并为它开一个后继 run；而且每个后继 run 只消费一条 | `core/chat/steer_queue.py:380–402`、`core/chat/runtime/handoff.py:62` |
| V5 | core 有空闲时合并唤醒的先例 | **成立**。同一 chat 的多条命令完成通知合并成一条隐藏输入，只开一个 run；chat 正忙时（`get_active_run`）不唤醒 | `core/sandbox/commands/delivery.py:65–94`、`notifications.dispatch_batch` |
| V6 | 群聊有发送者准入 | **不成立**。只判断是否 @ 了机器人，不判断发送者 | `integrations/channels/inbound.py:190`；`personal_agent/channels/channel_bridge.admit_inbound` 只加说话人前缀 |

由此得到的修订：
- **Router 不迁 core ChatRun**（§7.1）。Worker 迁，但要先做三处 core 小改动（§14 第 0 阶段）。
- **`inject` 不走 steer 队列**，改为写 `context_only` 的 ChatMessage（§6.2）。
- **G11 升级为独立的安全修复**，不等重构。

## 18. 待你决定的问题

| # | 问题 | 本文默认 | 备选 |
|---|---|---|---|
| D1 | Router 的执行内核 | 自建 Turn 内核（§7.1） | 迁 core ChatRun。需要改 `accept_main_run`，让它接受已存在的多条消息，并改 handoff 和历史加载，影响普通聊天 |
| D2 | Router 不再写 goal（`pa_route` + 原始 Trigger 引用） | 采纳 | 保留 `pa_task_create(goal)`，只加 `trigger_refs` |
| D3 | IM 回复 100 词硬上限 | 采纳，用近似计数，不引入 jieba | 只写在提示词里，不硬拦截；或者引入 jieba |
| D4 | Watchdog（独立进程、提示词版本表） | 放在第 6、7 阶段，与执行内核重构解耦，可以单独排期 | 先只做第 1 阶段的 HealthBoard，Watchdog 后续再定 |
| D5 | 群聊准入（V6） | 立刻单独修：非 owner 的群消息只作上下文，不启动任务 | 跟着第 5 阶段一起改 |
