# SSE 事件类型参考

流式聊天端点 `POST /v1/chats/stream` 返回 Server-Sent Events。后端把 run 跑在后台
（ChatRun + Redis Stream），SSE 只是"跟随"——断线后可用 `GET /v1/chats/stream/{run_id}`
重新接上同一条流续播。前端解析在 `hooks/useStreaming.ts`。

## 流格式

```
data: {"type": "<event_type>", ...payload}    # JSON 事件
data: [DONE]                                  # 流终止标记
: heartbeat                                   # SSE 注释行心跳（15s 一次，直接忽略）
```

> **没有 `text` / `done` 事件**（旧版概念）。文本增量是 `content`，结束是 `end` 事件或 `[DONE]`。

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

### `tool_call` — 工具调用开始

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

### `follow_up` — 追问建议

```json
{"type": "follow_up", "follow_up_questions": ["追问1", "追问2"]}
```

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
- `tool` → ToolCall (可展开，显示名称/参数/结果)
- `text` → Markdown 渲染 + 引用标记

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
