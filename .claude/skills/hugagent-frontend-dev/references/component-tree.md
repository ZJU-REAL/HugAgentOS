# 组件树参考

## 应用入口（main.tsx 按路径分发）

```
main.tsx
├── App.tsx              # 默认：主聊天应用
├── ApiDocApp.tsx        # /api-docs 开放 API 文档
└── SharePreviewApp.tsx  # ?share=   分享预览页
```

## 用户端 (App.tsx)

```
App.tsx
├── Sidebar
│   ├── Logo + 导航按钮 (NAV_ITEMS)
│   ├── 新建对话按钮
│   ├── 历史搜索 + 筛选 (时间/话题)
│   ├── 会话列表 (groupedHistoryList)
│   │   ├── 今天 / 昨天 / 近7天 / 更早
│   │   └── 每项: 标题 + 右键菜单(置顶/收藏/重命名/导出/删除)
│   └── 底部: 设置 + 文档
│
├── 主内容区
│   ├── Header (.jx-topbar / .jx-chatTopbar)
│   │
│   ├── ChatArea (panel === 'chat')
│   │   ├── 空状态 (EmptyPage)
│   │   │   ├── Hero 标题 + 副标题
│   │   │   ├── InputArea (居中, 840×148)
│   │   │   ├── 快捷场景 (.jx-quickPills)
│   │   │   └── 能力卡片 (.jx-capCards)
│   │   │
│   │   └── 对话模式
│   │       ├── 消息列表 (.jx-chatList)
│   │       │   └── MessageBubble × N
│   │       │       ├── 用户消息: 文本 + 附件
│   │       │       └── AI消息: segments[] 渲染
│   │       │           ├── ThinkingBlock (可折叠)
│   │       │           ├── ToolCall (可展开)
│   │       │           ├── Text + Markdown
│   │       │           ├── Citations (CitationBadge)
│   │       │           └── 追问建议 (followUpQuestions)
│   │       └── InputArea (.jx-inputArea, 底部固定)
│   │           ├── FileAttachmentCard × N (已选文件)
│   │           ├── Textarea (.jx-composer)
│   │           └── Toolbar (.jx-composerBar)
│   │               ├── 思考模式 (.jx-modeDropBtn)
│   │               ├── 提示词库 (.jx-promptHubBtn)
│   │               ├── 附件按钮 (.jx-attachBtn)
│   │               └── 发送按钮 (.jx-sendBtn)
│   │
│   ├── CatalogPanel (panel === 'skills'|'agents'|'mcp'|'kb')
│   │   ├── 搜索栏
│   │   ├── 项目列表 (Toggle 开关)
│   │   └── 详情弹窗 (Markdown 渲染)
│   │
│   └── DocsPanel (panel === 'docs')
│       ├── 版本更新 Tab
│       └── 能力介绍 Tab
│
├── 右侧面板
│   ├── ToolResultPanel (工具执行结果)
│   └── PromptHubPanel (提示词库)
│
└── 全局弹窗
    ├── SettingsModal (记忆、排序等)
    ├── CreateKBModal (知识库创建)
    ├── ReindexModal (重建索引)
    ├── ImagePreview (图片预览)
    └── AuthExpiredModal (登录过期)
```

## 数据流

```
用户操作
  → Store action (Zustand)
    → API call (authFetch)
      → Backend /v1/* endpoint
    ← ApiEnvelope<T> response
  ← set() 更新 Store
← 组件自动 re-render
```
