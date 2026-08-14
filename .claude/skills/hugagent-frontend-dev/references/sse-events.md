# SSE 事件类型参考

流式聊天端点 `POST /v1/chats/stream` 返回 Server-Sent Events。后端把 run 跑在后台
（ChatRun + Redis Stream），SSE 只是"跟随"——断线后可用 `GET /v1/chats/stream/{run_id}`
重新接上同一条流续播。

**前端解析单一真源是 `hooks/chatStream.ts`（`processChatStream()`）**；`hooks/useStreaming.ts`
只做发送/中止/续播/排队编排；通用分帧器在 `utils/sse.ts`（`parseSSE`）。后端事件产出主体在
`orchestration/streaming.py`（StreamingAgent）与 `orchestration/chat_run_executor.py`。

## 流格式

```
data: {"type": "<event_type>", ...payload}    # JSON 事件
data: [DONE]                                  # 流终止标记
: heartbeat                                   # SSE 注释行心跳（直接忽略）
```

> 没有独立的 `done` 事件——结束是 `end` 事件或 `[DONE]`。正文事件规范名是 `content`，
> 但 `ai_message` / `text` / `delta` 都会被当作正文别名接受。

## 事件类型

### `run_started` — 首帧

```json
{"type": "run_started", "run_id": "run_xxx", "message_id": "msg_xxx"}
```

- 必须记录到 `useChatStore.setActiveRun(chatId, { runId, messageId })`
- `run_id` 是断线续播的凭据

### `content` — 文本增量

```json
{"type": "content", "delta": "你好"}
```

- 取 `delta || content || text` 追加到当前消息内容
- 兼容别名：`ai_message` / `text` / `delta`
- 内嵌 `<think>...</think>` 标签的模型由 parseBuffer 解析为 thinking（structuredReasoning=false 时）

### `thinking` — 思考过程

```json
{"type": "thinking", "delta": "让我分析一下..."}
```

- 取 `content || text || delta`；有 `delta` 字段时按增量追加到最近的 thinking segment
- 收到带 `delta` 的 thinking 即标记 structuredReasoning（思考走独立通道，如 reasoning_content），关闭 `<think>` 内嵌解析
- 兼容别名：`thought`

### `content_replace` — 整段替换正文

```json
{"type": "content_replace", "content": "重写后的完整正文"}
```

- 本体治理评审后重写答案时使用：**整体替换**当前消息正文，而不是追加

### `tool_call_start` / `tool_call_delta` — 工具参数流式展示

```json
{"type": "tool_call_start", "id": "call_abc123", "name": "internet_search"}
{"type": "tool_call_delta", "id": "call_abc123", "delta": "{\"query\": \"北京"}
```

- 参数尚未生成完时先出卡片（status='running'），delta 增量拼接参数 JSON 实时展示
- 随后的 `tool_call` 带完整参数，按 id 更新同一张卡片

### `tool_call` — 工具调用（完整参数）

```json
{"type": "tool_call", "id": "call_abc123", "name": "internet_search", "input": {"query": "北京 天气"}}
```

- 参数字段取 `input ?? args ?? tool_args ?? arguments`
- 按工具 id 去重：已存在则更新（status='running'），否则 push 到 `toolCalls[]` 并追加 `{ type: 'tool', toolIndex }` segment
- 兼容别名：`tool_use` / `tool_start`

### `tool_result` — 工具执行结果

```json
{
  "type": "tool_result",
  "id": "call_abc123",
  "output": "搜索结果内容...",
  "citations": [{"title": "...", "url": "...", "source": "internet_search"}]
}
```

- 结果字段取 `output ?? result`；`error` 字段存在 → status='error'，否则 'success'
- `citations` 追加到 `allCitations[]`
- `subagent_name` 存在时改写 displayName 为「调用子智能体：xxx」
- 兼容别名：`tool_end`

### `tool_pending` — 工具等待批复

```json
{"type": "tool_pending"}
```

- UI 进入 pending 态；收到下一个非 heartbeat 事件自动解除

### `batch_confirm` — 批量计划确认

```json
{"type": "batch_confirm", "plan_id": "...", "total": 20, "source_type": "xlsx",
 "preview": [...], "default_template": "...", "placeholder_keys": [...], "chat_id": "...", "warnings": [...]}
```

- batch_runner MCP 返回了执行计划，后端已暂停 agent
- 调 `useBatchStore.setPendingConfirm(...)` 打开确认弹窗，用户审阅/编辑模板后才执行

### `file_confirm` — 我的空间写确认

```json
{"type": "file_confirm", "confirm_id": "...", ...}
{"type": "file_confirm", "confirm_id": "...", "expired": true}
```

- 某工具协程已挂起等用户确认 /myspace 写。**本 SSE 流不结束**——用户点允许/拒绝走带外
  `POST /file-confirm`，挂起的工具原地续跑，后续 tool_result/meta 仍从同一条流来
- 入队 `useUIStore.enqueuePendingConfirm(chatId, info)`；`expired: true` 表示该项超时回收，
  调 `resolvePendingConfirm` 只摘掉这一个 confirm_id

### `design_pick` — 建站方案三选一

- 与 `file_confirm` 同机制：流不结束，用户选择走带外接口，工具协程原地续跑

### `plan_update` — 计划清单更新

```json
{"type": "plan_update", "plan": [{"content": "...", "status": "completed"}, ...]}
```

- `update_plan` 工具产生的轻量计划清单；run 正常结束时后端自动补发全部 completed 的收尾帧

### `subagent_event` — 子智能体嵌套事件

```json
{"type": "subagent_event", "subType": "tool_call", ...}
```

- `subType` 取值：`tool_call` / `tool_call_delta` / `tool_result` / `thinking` / `content` / `error`
- 渲染为父工具卡片的 `subSteps` 子步骤

### `steer_applied` — 运行中插话生效

- 用户在 run 进行中追加的指令（steering）被后端采纳的边界标记

### `compaction_notice` — 上下文压缩提示

- 长会话触发上下文压缩时的提示帧

### `model_progress` — 模型活性信号

- 表示"模型还在飞行中"，用于喂 run 卡死看门狗/停滞检测，UI 不直接渲染

### `ontology_*` — 本体治理事件族

`ontology_activation` / `ontology_gate` / `ontology_review` / `ontology_repair` /
`ontology_revision` / `ontology_revision_thinking`——本体激活、工具闸、输出评审、修复与重写过程
（重写结果经 `content_replace` 落地）。另有 `eventObj.scope === 'ontology_revision'` 的分流。

### `follow_up` — 追问建议

```json
{"type": "follow_up", "follow_up_questions": ["追问1", "追问2"]}
```

- 追问也可能不走 SSE：流结束后由后台任务生成，写进消息 `extra_data.follow_up_questions`，
  前端轮询/刷新历史时取到

### `meta` — 元信息

```json
{"type": "meta", "message_id": "msg_xxx", "citations": [...], "workspace_files": [...], "artifacts": [...]}
```

- 设置 `messageId`；`citations` 非空时**整体替换** allCitations
- `artifacts` 追加为下载类 toolCall 展示

### `error` — 错误

```json
{"type": "error", "error": "错误描述"}
```

- 直接 throw，终止本次流处理

### `end` / `[DONE]` — 流结束

- `{"type": "end"}` 或 `data: [DONE]` 均表示结束
- finalize 所有 running 状态的 toolCalls，触发后续操作（摘要、分类等由后端负责）

## Segment 渲染顺序

消息通过 `segments[]` 按顺序渲染：

```
[thinking] → [tool] → [thinking] → [tool] → [text]
```

每个 segment 类型对应不同的 UI 组件：
- `thinking` → ThinkingBlock (可折叠)
- `tool` → ToolCall (可展开，显示名称/参数/结果；子智能体带 subSteps)
- `text` → Markdown 渲染 + 引用锚点胶囊（`[锚文本](cite:eN)`）
- `plan` → 计划清单条

## 中断流

```typescript
// AbortController 中断
const abortController = new AbortController();

// 发送请求时
const r = await authFetch(url, {
  method: 'POST',
  signal: abortController.signal,
  body: ...,
});

// 用户点击停止
abortController.abort();
```

## 断线续播

```typescript
// run_started 时已存下 activeRun
const { runId } = useChatStore.getState().activeRuns[chatId];

// 重连：从后台 run 的 Redis Stream 重放 + 继续跟随
const r = await authFetch(`${apiUrl}/v1/chats/stream/${runId}`);
// 事件格式与 POST /v1/chats/stream 完全一致
```

实际编排在 `useStreaming.ts::resumeRunIfAny`：

- `getActiveChatRun` 探测活跃 run；已终态则清僵尸状态（`cleanupZombieRunState`）
- 普通对话从 `last_event_offset` **断点续传**；`plan_generate` / `plan_execute` / `autonomous_loop`
  从 offset 0 **全量重放**（loop 会先截掉尾部 assistant 占位再重建）
- 跨标签页互斥：Web Locks `hugagent_run_follow_${run_id}`（`ifAvailable: true`），
  只允许一个标签页跟随同一 run
- 运行中插话：`steerChatRun` / `withdrawChatRunSteer`，失败时降级为下一轮普通消息
