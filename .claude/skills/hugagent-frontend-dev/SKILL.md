---
name: hugagent-frontend-dev
description:
  HugAgentOS 前端开发规范。当需要新增或修改前端代码（组件、Store、Hook、API调用、样式等）时使用此 skill，
  确保组件结构、状态管理、样式命名、类型安全等与项目现有规范保持一致。
---

# HugAgentOS 前端开发规范

本 skill 定义了 HugAgentOS 项目前端（React + TypeScript + Ant Design + Zustand）的开发规范与流程。
所有前端代码变更必须遵守以下规范。

## 文件索引

### 模板 (`templates/`)
| 文件 | 用途 |
|------|------|
| `component.tsx` | React 组件模板（Props + Hooks + 错误处理） |
| `store.ts` | Zustand Store 模板（CRUD + localStorage 持久化） |
| `hook.ts` | 自定义 Hook 模板（初始化 + 清理 + abort） |
| `css-module.css` | CSS 样式模板（.jx- 前缀 + 响应式） |

### 参考文档 (`references/`)
| 文件 | 内容 |
|------|------|
| `ui-design-spec.md` | **UI 设计规范**（颜色系统、字体排版、图标、圆角、间距、组件规范、Ant Design 主题映射） |
| `icon-assets.md` | **图标资产清单**（全部 SVG 图标索引、使用方式、分类说明） |
| `component-tree.md` | 完整组件树 |
| `css-variables.md` | CSS 变量、命名规则、常用尺寸、样式模式（与 UI 设计规范对齐） |
| `api-patterns.md` | API 调用模式（authFetch、SSE） |
| `sse-events.md` | SSE 事件类型详解（run_started/content/thinking/tool_call/tool_result/tool_pending/batch_confirm/file_confirm/meta/error + [DONE]） |

### 图标资产 (`assets/`)
| 目录 | 内容 |
|------|------|
| `assets/icons/` | 通用 UI 图标（30 个 SVG：logo、导航、操作按钮、场景图标等） |
| `assets/mcp-icons/` | MCP 工具库图标（12 个 SVG：互联网、数据、报告、知识等） |

### 脚本 (`scripts/`)
| 文件 | 用途 |
|------|------|
| `scaffold_component.sh` | 一键生成新组件（.tsx + .css + index.ts） |

> 模板中 `${ComponentName}` / `${componentName}` / `${group}` 为占位符，使用时替换。

---

## 1. 目录结构

```
src/frontend/src/
├── main.tsx                   # 入口：按路径分发应用壳
├── App.tsx                    # 主聊天应用（用户端）
├── ApiDocApp.tsx              # /api-docs 开放 API 文档
├── SharePreviewApp.tsx        # ?share= 分享预览页
├── types.ts                   # 共享 TypeScript 类型
├── api.ts                     # API 客户端（信封解包）
├── storage.ts                 # localStorage 工具 + defaultCatalog
│
├── components/                # 组件组（每组 index.ts barrel export）
│   ├── chat/                  # ChatArea, InputArea, MessageBubble, PromptHubPanel
│   ├── agent/                 # 子智能体
│   ├── apidoc/                # 开放 API 文档
│   ├── automation/            # 定时任务/自动化
│   ├── batch/                 # 批量执行
│   ├── canvas/                # 画布产物
│   ├── catalog/               # CatalogPanel（能力中心）
│   ├── citation/              # CitationBadge, CitationHtmlBlock, CitationMarkdownBlock
│   ├── common/                # AuthExpiredModal, ImagePreview 等
│   ├── docs/                  # DocsPanel（版本说明/能力介绍）
│   ├── file/                  # FileAttachmentCard
│   ├── kb/                    # 知识库（CreateKBModal, ReindexModal 等）
│   ├── lab/                   # 实验室（技能蒸馏等）
│   ├── memory/                # 记忆中心
│   ├── myspace/               # 我的空间
│   ├── projects/              # 项目空间
│   ├── settings/              # SettingsModal（含记忆 L1/L2/L3 Tab）
│   ├── share/                 # 分享
│   ├── sidebar/               # Sidebar
│   └── tool/                  # ToolOutputRenderer, ToolResultPanel
│
├── hooks/                     # 7 个自定义 Hook
│   ├── useChatInit.ts         # 认证、Catalog、会话、消息初始化
│   ├── useChatActions.ts      # 聊天 CRUD、重命名、导出
│   ├── useStreaming.ts        # SSE 流式处理、文件上传、发送逻辑、断线续播
│   ├── usePlanMode.ts         # Plan 模式
│   ├── usePageConfig.ts       # 页面配置
│   ├── useDelayedFlag.ts      # 延迟标志位
│   └── useStallDetector.ts    # 流式停滞检测
│
├── stores/                    # 18 个 Zustand Store
│   ├── authStore.ts           # 认证状态
│   ├── chatStore.ts           # 聊天会话、消息、activeRun
│   ├── catalogStore.ts        # Catalog（技能、智能体、MCP、知识库）
│   ├── uiStore.ts             # UI 面板、搜索、筛选、pending confirm 队列
│   ├── settingsStore.ts       # 记忆、排序、偏好
│   ├── fileStore.ts           # 文件上传
│   ├── kbStore.ts             # 知识库管理
│   ├── agentStore.ts          # 子智能体
│   ├── automationStore.ts / automationChatStore.ts  # 自动化
│   ├── batchStore.ts          # 批量执行（pendingConfirm）
│   ├── canvasStore.ts         # 画布
│   ├── editionStore.ts        # CE/EE 门控（消费 /v1/meta/edition）
│   ├── modelCapabilitiesStore.ts  # 模型能力探测
│   ├── mySpaceStore.ts        # 我的空间
│   ├── pageConfigStore.ts     # 页面配置
│   ├── projectStore.ts        # 项目空间
│   └── skillDistillStore.ts   # 技能蒸馏
│
├── utils/                     # 20+ 工具模块
│   ├── citations.ts           # 引用解析与去重
│   ├── constants.ts           # 工具名称、快捷场景、能力卡片
│   ├── export.ts              # PDF 导出
│   ├── fileParser.ts          # 文件解析、OSS 上传
│   ├── history.ts             # 话题推断、日期分组
│   ├── highlight.ts           # 代码高亮
│   ├── markdown.ts            # Markdown 渲染
│   ├── segments.ts            # 消息分段（thinking/tool/text）
│   ├── apiError.ts / avatar.ts / confirmDelete.ts / date.ts / fileIcon.ts /
│   │   folderTree.ts / iconLibrary.ts / pageConfigDefaults.ts / roles.ts /
│   │   scroll.ts / xlsxRange.ts / codeExecParser.ts / codeExecUtils.ts
│   └── index.ts               # Barrel export
│
├── styles/                    # 18 个 CSS 模块
│   ├── variables.css          # CSS 自定义属性（颜色、阴影）
│   ├── common.css             # 全局工具类
│   ├── chat.css / sidebar.css / catalog.css / tool.css
│   ├── automation.css / automation-timeline.css / batch（并入相关文件）
│   ├── canvas.css / config.css / mcp.css / myspace.css / plan.css
│   └── projects.css / search-modal.css / settings.css / skill-distill.css / team-folder.css
```

**核心原则：** 按功能分组，每组有 `index.ts` barrel export。

---

## 2. 组件规范

### 2.1 组件文件结构

```typescript
import React, { useState, useMemo, useCallback, useRef } from 'react';
import { Button, Input, message } from 'antd';
import { useChatStore } from '../../stores';
import type { ChatMessage } from '../../types';
import './styles.css';  // 如有独立样式

// 1. Props 接口（以 Props 结尾）
interface MyComponentProps {
  chatId: string;
  onClose: () => void;
  items?: SomeItem[];
}

// 2. 函数式组件（named export）
export function MyComponent({ chatId, onClose, items = [] }: MyComponentProps) {
  // 3. Hooks 在顶层调用
  const { store, currentChatId } = useChatStore();
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // 4. useMemo 用于派生状态
  const filteredItems = useMemo(() => {
    return items.filter(item => item.active);
  }, [items]);

  // 5. useCallback 用于稳定引用
  const handleSubmit = useCallback(async () => {
    setLoading(true);
    try {
      await someApiCall();
      message.success('操作成功');
    } catch (e) {
      message.error(`操作失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [chatId]);

  // 6. Early return 处理空状态
  if (!items.length) {
    return <div className="jx-empty">暂无数据</div>;
  }

  // 7. JSX 渲染
  return (
    <div className="jx-myComponent">
      {filteredItems.map(item => (
        <div key={item.id} className="jx-myComponent-item">
          {item.name}
        </div>
      ))}
      <Button onClick={handleSubmit} loading={loading}>提交</Button>
    </div>
  );
}
```

### 2.2 组件规则

- **命名**: PascalCase，一个文件一个组件
- **导出**: 使用 named export（`export function`），非 default export
- **Props**: 必须定义 TypeScript 接口，以 `Props` 结尾
- **Hooks**: 必须在组件顶层调用，不能在条件/循环中
- **状态**: 全局状态用 Zustand Store，局部状态用 useState
- **性能**: 用 `useMemo` 缓存计算，`useCallback` 稳定函数引用
- **错误处理**: try-catch + `message.error()` 提示用户
- **注册**: 新组件必须在对应目录的 `index.ts` 中 export

---

## 3. Zustand Store 规范

### 3.1 Store 定义模板

```typescript
import { create } from 'zustand';

// 1. 定义 State 接口（状态 + Actions）
interface MyState {
  // 状态字段
  items: Item[];
  loading: boolean;
  selectedId: string | null;

  // Actions
  setItems: (items: Item[]) => void;
  setLoading: (loading: boolean) => void;
  selectItem: (id: string | null) => void;
  fetchItems: () => Promise<void>;
}

// 2. 创建 Store
export const useMyStore = create<MyState>((set, get) => ({
  // 初始状态
  items: [],
  loading: false,
  selectedId: null,

  // 简单 setter
  setItems: (items) => set({ items }),
  setLoading: (loading) => set({ loading }),
  selectItem: (id) => set({ selectedId: id }),

  // 异步操作
  fetchItems: async () => {
    set({ loading: true });
    try {
      const data = await getItemsApi();
      set({ items: data });
    } catch (e) {
      console.error('Failed to fetch items:', e);
    } finally {
      set({ loading: false });
    }
  },
}));
```

### 3.2 Store 使用方式

```typescript
// 在组件中（自动订阅 re-render）
const { items, loading, fetchItems } = useMyStore();

// 在回调/事件中（不触发 re-render，用于读取最新值）
const currentItems = useMyStore.getState().items;

// 复杂更新（使用 updater 函数）
useChatStore.getState().updateStore((prev) => ({
  chats: { ...prev.chats, [id]: updatedChat },
  order: [id, ...prev.order.filter(x => x !== id)],
}));
```

### 3.3 Store 规则

- 接口驱动：先定义 interface，再实现
- 状态与 Actions 在同一接口中
- 简单更新用 `set()`，复杂逻辑用 `get()` 读取当前值
- 需要持久化的状态在 setter 中调用 `localStorage` 保存
- Set/Map 类型用于内部状态（如 `expandedToolCalls: Set<string>`）

---

## 4. 自定义 Hook 规范

```typescript
// Hook 命名以 use 开头
export function useMyFeature(apiUrl: string) {
  const { someState } = useSomeStore();
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // useEffect 按关注点分离
  useEffect(() => {
    // 初始化逻辑
    return () => {
      // 清理
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  useEffect(() => {
    // 响应依赖变化
  }, [someState, apiUrl]);

  // 返回对象（函数 + refs）
  return {
    doSomething,
    timerRef,
  };
}
```

**规则：**
- Hook 文件放在 `hooks/` 目录
- 复杂初始化逻辑提取到 Hook 中，保持组件简洁
- 在 `hooks/index.ts` 中 barrel export

---

## 5. API 调用规范

### 5.1 API 客户端（api.ts）

```typescript
// 信封类型
interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
  trace_id?: string;
  timestamp?: number;
}

// 带认证的 fetch
export async function authFetch(url: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers || {});
  const token = getAuthToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(url, { ...init, headers });
  if (response.status === 401) onUnauthorized?.(loginUrl);
  return response;
}

// 使用示例
const r = await authFetch(`${apiUrl}/v1/catalog`);
const { code, data } = await r.json();
```

### 5.2 规则

- 所有 API 调用通过 `authFetch()`
- 请求路径使用 `/v1/` 前缀
- 响应解包：检查 `code` 字段，取 `data`
- 401 响应自动触发登录弹窗

---

## 6. TypeScript 类型规范

### 6.1 核心类型（types.ts）

```typescript
// 面板导航
export type PanelKey =
  | 'chat' | 'skills' | 'agents' | 'mcp' | 'kb' | 'docs'
  | 'app_center' | 'settings' | 'share_records' | 'my_space'
  | 'ability_center' | 'lab' | 'projects' | 'project_detail';

// 聊天消息
export interface ChatMessage {
  role: ChatRole;
  content: string;
  isMarkdown?: boolean;
  ts: number;
  messageId?: string;
  toolCalls?: ToolCall[];
  thinking?: ThinkingBlock[];
  segments?: MessageSegment[];
  citations?: CitationItem[];
  followUpQuestions?: string[];
  attachments?: Array<{ name: string; mime_type?: string; file_id?: string; download_url?: string }>;
}

// Catalog 项
export interface CatalogItemBase {
  id: string;
  name: string;
  desc: string;
  enabled: boolean;
  tags?: string[];
  detail?: string;
}
```

### 6.2 类型规则

- 所有共享类型定义在 `types.ts`
- 组件 Props 接口定义在组件文件中
- 使用 `interface` 而非 `type`（除非需要联合类型）
- 避免 `any`，必要时用 `unknown` + 类型守卫
- 枚举类型用 `type = 'a' | 'b' | 'c'` 字面量联合

---

## 7. CSS 样式规范

> **完整设计规范**: 参见 `references/ui-design-spec.md`，包含颜色系统、字体排版、图标、圆角、间距等全部视觉标准。

### 7.1 命名规则

```css
/* 所有类名以 .jx- 为前缀 */
.jx-chatArea { }
.jx-chatArea-header { }       /* Element: 连字符 */
.jx-chatArea-content { }
.jx-chatArea--empty { }       /* Modifier: 双连字符 */
```

### 7.2 核心颜色（摘要）

```css
:root {
  /* 主色 — 蓝色系 */
  --color-primary: #126DFF;
  --color-primary-hover: #3C87FF;
  --color-primary-active: #0862F3;
  --color-primary-bg: #DBE9FF;
  --color-primary-light: #EBF2FF;

  /* 文字 */
  --color-text: #262626;           /* 标题/强调 */
  --color-text-secondary: #4D4D4D; /* 正文内容 */
  --color-text-tertiary: #808080;  /* 次要信息 */
  --color-text-placeholder: #B3B3B3; /* 置灰/说明 */

  /* 边框与填充 */
  --color-border: #E3E6EA;
  --color-fill: #D8DBE2;
  --color-bg-gray: #F5F6F7;

  /* 语义色 */
  --color-success: #02B589;
  --color-warning: #F8AB42;
  --color-error: #FC5D5D;

  /* 圆角 */
  --radius-xs: 4px;   /* 标签 */
  --radius-sm: 8px;   /* 按钮、输入框 */
  --radius-md: 12px;  /* 卡片 */
  --radius-lg: 20px;  /* 弹窗 */

  /* 间距 (4px 基准) */
  --space-xs: 4px;  --space-sm: 8px;
  --space-md: 16px; --space-lg: 20px; --space-xl: 40px;

  /* 字体 */
  --font-family: "PingFang SC", "Microsoft YaHei", "微软雅黑", sans-serif;
  --font-family-number: "DingTalk Sans", "PingFang SC", "Microsoft YaHei", sans-serif;
}
```

### 7.3 样式规则

- **前缀**: 所有自定义类使用 `.jx-` 前缀
- **BEM**: 子元素用 `-`，修饰符用 `--`
- **颜色**: 必须使用 CSS 变量，禁止硬编码色值
- **间距**: 以 4px 为基准，常用 4/8/16/20/40px
- **圆角**: 按组件类型使用对应层级（4/8/12/20px）
- **字号**: 12/14/16/18/22/44px，默认正文 14px
- **字体**: 中文 PingFang SC / 微软雅黑，特殊数字用 DingTalk Sans
- **Icon**: 推荐 IconPark 图标库，蓝色系为主
- **布局**: Flexbox 为主
- **文件**: 按功能放在 `styles/` 对应文件中
- **Ant Design**: 通过 ConfigProvider token 定制主题色，不覆写组件内部样式（详见 `ui-design-spec.md` 第 13 节）

---

## 8. SSE 流式处理规范

```typescript
// useStreaming.ts 中的 SSE 事件处理模式（详见 references/sse-events.md）
const reader = response.body.getReader();
const decoder = new TextDecoder('utf-8');
let sseBuffer = '';

// 事件类型处理（注意：没有 text/done 事件！）
// 'run_started'   → 首帧，记录 run_id/message_id 到 chatStore.activeRun（断线续播用）
// 'content'       → 文本 delta 增量，追加到消息内容（兼容别名 ai_message/text/delta）
// 'thinking'      → 思考过程，追加到 thinking segments
// 'tool_call'     → 工具调用，追加到 toolCalls 数组 + segments（兼容 tool_use/tool_start）
// 'tool_result'   → 工具结果，更新对应 toolCall 的 output/status + citations（兼容 tool_end）
// 'tool_pending'  → 工具等待批复，UI 进入 pending 态
// 'batch_confirm' → 批量计划确认 → batchStore.setPendingConfirm 弹确认框
// 'file_confirm'  → /myspace 写确认 → uiStore.enqueuePendingConfirm（流不结束，带外 POST 批复）
// 'follow_up'     → 追问建议 follow_up_questions
// 'meta'          → message_id、citations、workspace_files、artifacts
// 'error'         → 抛错终止
// 'end' / 'data: [DONE]' → 流结束；心跳是 SSE 注释行（15s）
```

断线续播：`run_started` 给出的 `run_id` 可用 `GET /v1/chats/stream/{run_id}` 重新跟随后台 run。

---

## 9. 文件上传规范

```typescript
// 并行处理：解析内容 + 上传到 OSS
const promise = Promise.all([
  parseFileContent(file, apiUrl),      // 二进制 → 后端解析，文本 → FileReader
  uploadFileToOSS(file, apiUrl, chatId), // FormData → /v1/file/upload
]).then(([content, { file_id, download_url }]) => ({
  name: file.name,
  content,
  file_id,
  download_url,
  mime_type: file.type,
}));
```

---

## 10. localStorage 持久化

```typescript
// 存储键命名规范
export const STORAGE_KEY = 'hugagent_ui_chat_history_v2';   // 聊天数据
export const ENABLE_KEY = 'hugagent_ui_enabled_catalog_v1'; // Catalog 状态

// ID 生成
export function nowId(prefix = 'chat') {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${prefix}_${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

// Store 中自动保存
setStore: (store) => {
  set({ store });
  saveChatStore(store);  // → localStorage.setItem(STORAGE_KEY, JSON.stringify(store))
},
```

---

## 11. 状态管理模式

### 乐观更新

```typescript
// catalogStore.ts
toggleItem: async (kind, itemId, enabled) => {
  const { catalog } = get();
  // 1. 先更新本地状态（乐观）
  const updated = {
    ...catalog,
    [kind]: catalog[kind].map(item =>
      item.id === itemId ? { ...item, enabled } : item,
    ),
  };
  set({ catalog: updated });
  saveCatalog(updated);

  // 2. 后端同步（最大努力）
  try {
    await updateCatalogItem(kind, itemId, enabled);
  } catch (e) {
    console.error('Sync failed:', e);
  }
},
```

### 派生状态（useMemo）

```typescript
const filteredList = useMemo(() => {
  return list
    .filter(item => matchesFilter(item, filter))
    .sort((a, b) => b.updatedAt - a.updatedAt);
}, [list, filter]);
```

---

## 12. Nginx 代理配置

```nginx
# SPA 路由回退
location / {
  try_files $uri $uri/ /index.html;
}

# API 代理（支持 SSE）
location /api/ {
  proxy_pass http://backend:${BACKEND_PORT}/;
  client_max_body_size 50m;
  proxy_buffering off;      # SSE 必须关闭缓冲
  proxy_cache off;
  proxy_read_timeout 300s;
}

# 静态资源（长期缓存）
location /assets/ {
  add_header Cache-Control "public, max-age=31536000, immutable" always;
}

# index.html（不缓存）
location = /index.html {
  add_header Cache-Control "no-store, no-cache, must-revalidate" always;
}
```

---

## 13. Docker 开发流程

```bash
# 完整重建（慢但保证正确）
docker-compose up -d --build frontend

# 本地构建 + 热替换（快，需要 Node 20+）
cd src/frontend
npm run build
docker cp dist/. hugagent-frontend:/usr/share/nginx/html/
docker exec hugagent-frontend nginx -s reload

# 前端 lint
cd src/frontend && npm run lint
```

---

## 14. 新功能开发检查清单

- [ ] 组件文件使用 PascalCase 命名
- [ ] Props 定义了 TypeScript 接口
- [ ] 使用 named export
- [ ] 全局状态通过 Zustand Store 管理
- [ ] API 调用通过 `authFetch()`
- [ ] CSS 类名使用 `.jx-` 前缀
- [ ] 颜色使用 CSS 变量（`--color-primary` 等），禁止硬编码色值
- [ ] 圆角使用分层变量（`--radius-xs/sm/md/lg`）
- [ ] 间距以 4px 为基准（`--space-xs/sm/md/lg/xl`）
- [ ] 字号使用规范层级（12/14/16/18/22/44px）
- [ ] 中文字体 PingFang SC / 微软雅黑，特殊数字用 DingTalk Sans
- [ ] 错误处理：try-catch + `message.error()`
- [ ] 组件在 `index.ts` 中 barrel export
- [ ] 类型定义完整（无 `any`）
- [ ] useMemo/useCallback 用于性能优化
- [ ] 使用 Ant Design 组件库 + ConfigProvider 主题配置
- [ ] Icon 优先使用 IconPark 图标库，蓝色系为主色调
- [ ] `npm run lint` 通过
