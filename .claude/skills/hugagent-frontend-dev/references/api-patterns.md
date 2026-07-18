# API 调用模式参考

## 核心 API 客户端 (api.ts)

### 信封解包

```typescript
interface ApiEnvelope<T> {
  code: number;      // 10000 = 成功
  message: string;
  data: T;
  trace_id?: string;
  timestamp?: number;
}

// 检查 + 解包
function unwrapData<T>(payload: unknown): T {
  if (isApiEnvelope<T>(payload)) return payload.data;
  return payload as T;
}
```

### authFetch — 带认证的 fetch

```typescript
import { authFetch } from '../api';

// GET
const r = await authFetch(`${apiUrl}/v1/items`);
const { code, data } = await r.json();

// POST
const r = await authFetch(`${apiUrl}/v1/items`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ name: 'test' }),
});

// DELETE
await authFetch(`${apiUrl}/v1/items/${id}`, { method: 'DELETE' });

// FormData (文件上传，不要设 Content-Type)
const form = new FormData();
form.append('file', file);
const r = await authFetch(`${apiUrl}/v1/file/upload`, {
  method: 'POST',
  body: form,
});
```

### 401 自动处理

`authFetch` 检测到 401 响应时自动调用 `onUnauthorized()`，触发登录弹窗。

---

## SSE 流式 (useStreaming.ts)

```typescript
// 发送聊天请求
const r = await authFetch(`${apiUrl}/v1/chats/stream`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    chat_id: currentChatId,
    message: input,
    model_name: selectedModel,
    attachments: [...],
  }),
});

// 读取 SSE 流
const reader = r.body!.getReader();
const decoder = new TextDecoder('utf-8');
let sseBuffer = '';

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  sseBuffer += decoder.decode(value, { stream: true });

  // 按行解析 SSE
  const lines = sseBuffer.split('\n');
  sseBuffer = lines.pop()!;

  for (const line of lines) {
    if (!line.startsWith('data: ')) continue;
    const payload = line.slice(6).trim();
    if (payload === '[DONE]') { /* 流结束 */ break; }
    const event = JSON.parse(payload);

    switch (event.type) {
      case 'run_started':   // { run_id, message_id } → 存 activeRun（断线续播）
      case 'content':       // { delta: "..." } 文本增量（无 text/done 事件！）
      case 'thinking':      // { delta | content: "..." }
      case 'tool_call':     // { id, name, input }
      case 'tool_result':   // { id, output, citations? }
      case 'tool_pending':  // 工具等待批复
      case 'batch_confirm': // 批量计划确认 → batchStore
      case 'file_confirm':  // /myspace 写确认 → uiStore（流不结束）
      case 'follow_up':     // { follow_up_questions: [...] }
      case 'meta':          // { message_id, citations, workspace_files, artifacts }
      case 'error':         // { error } → throw
      case 'end':           // 流结束
    }
  }
}
```

完整事件语义见 `sse-events.md`。断线续播：`GET /v1/chats/stream/{run_id}`。

---

## 常用 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/catalog` | 获取 Catalog（技能/智能体/MCP/KB） |
| POST | `/v1/chats/stream` | 流式聊天 |
| GET | `/v1/chats/stream/{run_id}` | 断线续播（重新跟随后台 run） |
| GET | `/v1/meta/edition` | CE/EE 版本与能力位（editionStore 消费） |
| GET | `/v1/chats` | 会话列表 |
| POST | `/v1/chats` | 创建会话 |
| GET | `/v1/chats/{id}/messages` | 历史消息 |
| PUT | `/v1/chats/{id}` | 更新会话（重命名/置顶） |
| DELETE | `/v1/chats/{id}` | 删除会话 |
| POST | `/v1/file/parse` | 解析文件内容 |
| POST | `/v1/file/upload` | 上传文件到 OSS |
| GET | `/v1/content/docs` | 版本更新 + 能力介绍 |
| GET | `/v1/memories` | 记忆列表 |
| DELETE | `/v1/memories/{id}` | 删除记忆 |
| GET | `/v1/auth/user` | 当前用户信息 |
