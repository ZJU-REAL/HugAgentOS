# SSE 事件类型参考

统一回复端点 `POST /v1/agents/responses`（`stream: true` 返回 SSE，默认返回完整 JSON）。后端把每一轮跑成后台 run
（`core/runs` 持有租约、事件日志与续播；`core/chat/runtime/worker.py` 消费事件并持久化输出），SSE 只是"跟随"。

| 端 | 位置 |
|---|---|
| 前端解析单一真源 | `hooks/chatStream.ts` → `hooks/chatStream/`（`consume.ts` 入口；`payload.ts` 分帧与分派；`lifecycleAndTools.ts` / `toolResultsAndMeta.ts` / `controls.ts` 分领域 reducer；`snapshot*.ts` 快照恢复） |
| 前端发送 / 续播编排 | `hooks/useStreaming.ts` + `hooks/streamSend.ts` / `streamFollowing.ts` / `streamRecovery.ts` / `streamQueue.ts` |
| 通用分帧器 | `utils/sse.ts`（`parseSSE`） |
| 订阅客户端 | `runSubscriptionClient.ts`（`GET /v1/chats/stream/{run_id}/subscription`） |
| 后端事件产出 | `core/agent_runtime/stream_main.py` / `stream_direct.py`、`core/llm/runtime/stream_events.py`；SSE 输出 `api/transports/run_sse.py` |

**新增事件处理只写在 `hooks/chatStream/` 对应的 reducer 里**，不要在别处再解析 SSE。

## 流格式

```
data: {"type": "<event_type>", "server_ts": 1730000000000, "event_offset": 42, ...payload}
data: [DONE]          # 流终止
: heartbeat           # 注释行心跳，忽略
```

- `server_ts` 更新最后活动时间（停滞检测）；`event_offset` 记录续播偏移。
- 无 `type` 的裸文本帧当作正文追加。
- 正文规范名 `content`，`ai_message` / `text` / `delta` 均作别名接受。

## 订阅帧（续播 / 刷新后）

| 事件 | 说明 |
|---|---|
| `run_snapshot` | 订阅首帧，携带 `state`：对齐的原生快照（已产生的正文/思考/工具段），`installRunSnapshot()` 直接装入 |
| `run_terminal` | run 已终态，结束跟随 |

## 生命周期

| 事件 | 载荷要点 | 处理 |
|---|---|---|
| `run_started` | `run_id`, `message_id` | 首帧，存 activeRun（续播凭据） |
| `queued_run_started` | 排队消息交接信息 | 排队中的下一条消息开始执行（`parseQueuedRunHandoff`） |
| `steer_applied` | 插话边界 | 运行中插话被采纳，切分正文边界 |
| `meta` | `message_id`, `citations`, `workspace_files`, … | 设置 messageId；`citations` 非空时整体替换 |
| `follow_up` | `follow_up_questions[]` | 追问建议；也可能流结束后后台生成写入消息 `extra_data` |
| `error` | `error` | 终止本次流处理 |
| `end` / `[DONE]` | — | 收尾所有 running 工具，标记流结束 |

## 正文与思考

| 事件 | 处理 |
|---|---|
| `content`（别名 `ai_message` / `text` / `delta`） | 取 `delta || content || text` 追加；内嵌 `<think>` 的模型由 parseBuffer 拆出思考 |
| `thinking`（别名 `thought`） | 带 `delta` 时按增量追加到最近的 thinking 段，并标记 structuredReasoning |
| `content_replace` | **整体替换**当前正文（本体治理重写答案） |

## 工具

| 事件 | 载荷要点 | 处理 |
|---|---|---|
| `tool_call_start`（兼容 `tool_use` / `tool_start`） | `id`, `name` | 先出运行中的工具行 |
| `tool_call_delta` | `id`, `arguments_delta` | 参数 JSON 增量拼接实时展示 |
| `tool_call` | `id`, `name`, `input`（兼容 `args` / `tool_args` / `arguments`） | 按 id 更新同一项 |
| `tool_progress` | `id`, `progress`, `total`, `message` | 运行中工具的进度注记（`progressNote`，如"3/10"） |
| `tool_result`（兼容 `tool_end`） | `id`, `output`/`result`, `error`, `citations` | 置 success / error；`subagent_name` 存在时改写展示名 |
| `tool_pending` | — | 工具等待批复，下一个非心跳事件自动解除 |
| `subagent_event` | `subType`: tool_call / tool_call_delta / tool_result / thinking / content / error | 渲染为父工具的 `subSteps` |
| `vision_progress` | `status`, `count` | 视觉桥在模型开口前读图，写 `chatStore.setVisionReading` |

> **UI 规范（v2）**：工具调用默认是**一行安静的文字**（做了什么 · 结果多少 · 用了多久），点击展开；运行中用文字微光，
> 完成用 `statusIn` 落定图标。只有文件、表格、网页等产物做成卡片。见 `ui-design-spec.md` §7.3 / §8.6。

## 需要用户介入（流不结束，带外批复后原地续跑）

| 事件 | 处理 |
|---|---|
| `file_confirm`（`confirm_id`；`expired: true` 表示超时回收） | 入队 `useUIStore.enqueuePendingConfirm`；用户允许/拒绝走带外接口 |
| `design_pick` / `choose_design` 工具 | 建站三选一卡片，同 file_confirm 机制；超时由助手自行选择 |
| `user_question`（`request_id`, `questions[]`） | `ask_user_question` 挂起 run，常驻输入区被问答面板替换；`enqueuePendingUserQuestion`，按 `request_id` 去重 |
| `user_question_resolved`（`request_id`, `outcome`） | **后端拥有终态**：在这里摘除；`outcome === 'timeout'` 时提示采用默认方案 |

> v2：这些"等你确认"项是**唯一允许扩散涟漪**的状态。

## 上下文与压缩

| 事件 | 处理 |
|---|---|
| `context_usage` | 上下文用量快照 → `chatStore.setContextUsage`（ContextGauge） |
| `compaction_progress` | 压缩进度（`phase` 如 `post_turn` = 本轮结束后执行的压缩）→ upsert `compaction` 段 |
| `compaction_notice` | 上一轮后台压缩完成的提示 → `chatStore.setContextCompaction` |

## 计划 / 循环

- `plan_update`：`update_plan` 工具产生的轻量计划清单；run 正常结束时后端补发全部 completed 的收尾帧。
- Plan 模式（`plan_step_*` / `plan_complete` 等）与自主循环（`loop_*`）事件经 `ChatStreamOptions.onEvent` 钩子交给
  `usePlanMode` / `useLoopMode` 先处理，钩子返回 true 则不再进通用 reducer。

## 本体治理

`ontology_activation` / `ontology_gate` / `ontology_review` / `ontology_repair` / `ontology_revision` /
`ontology_revision_thinking`；`scope === 'ontology_revision'` 的工具事件单独分流（重写结果经 `content_replace` 落地）。

## 已退役（不要再处理或产出）

- `batch_confirm`（批量执行已退役）
- `model_progress`（活性信号改由 `server_ts` / 心跳承担）

## Segment 渲染顺序

`segments[]` 按到达顺序渲染：`thinking` / `tool` / `text` / `plan` / `compaction`。

- `thinking` → 一行"已思考 N 秒"，可折叠
- `tool` → 一行过程记录，可展开（子智能体带 subSteps）
- `text` → Markdown + 引用锚点胶囊（`[锚文本](cite:eN)`）
- `plan` → 计划清单条
- `compaction` → 压缩过程行

## 断线续播

`hooks/streamRecovery.ts`：

- 探测活跃 run（`last_event_offset`）；已终态则清理僵尸状态
- 普通对话：`openRunSubscription(runId)` → `run_snapshot` 首帧恢复已产生内容，再继续跟随
- `plan_generate` / `plan_execute` / `autonomous_loop`：从 offset 0 全量重放
- 跨标签页互斥：Web Locks `hugagent_run_follow_${run_id}`，只允许一个标签页跟随同一 run
- 运行中插话：`steerChatRun` / `withdrawChatRunSteer`，失败时降级为下一轮普通消息

中断：`AbortController.abort()` 只断开跟随；取消 run 走显式取消接口（`markRunCancelledByUser`）。
