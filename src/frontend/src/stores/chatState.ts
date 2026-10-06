import type { ChatItem, ChatMessage, ChatStore as ChatStoreData, ContextCompactionState, ContextUsageSnapshot, PlanProgressState } from '../types';
import { type ChatInvocationContext } from '../utils/chatInvocation';

export type ChatMode = 'turbo' | 'fast' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';

export interface QueuedChatMessage {
  id: string;
  content: string;
  createdAt: number;
  status: 'queued' | 'steering' | 'applied';
  /** Explicit skill/plugin/connector/agent chips captured with this queued turn. */
  invocation?: ChatInvocationContext;
  appliedMessageId?: string;
  /** Run that durably accepted this card; retained across refresh for status reconciliation. */
  targetRunId?: string;
  /** Exact database queue state. The compact card maps it to three visual states. */
  durableStatus?: 'accepted' | 'claimed' | 'applied' | 'cancelled' | 'superseded';
}

export type ThinkingEffort = Exclude<ChatMode, 'turbo'>;

export interface ChatState {
  /** ID of the currently authenticated user. Null until `hydrateForUser` is
   *  called after login. All localStorage reads/writes are scoped by this id
   *  so different accounts on the same browser never share chat data. */
  currentUserId: string | null;
  /** All chat sessions keyed by id */
  store: ChatStoreData;
  /** Ref-like mutable mirror of store for use in closures */
  storeRef: ChatStoreData;
  /** Currently active chat id */
  currentChatId: string;
  /** Whether the *current* chat is streaming (derived from sendingChatIds) */
  sending: boolean;
  /** Set of chat IDs that are currently streaming responses.
   *  Multiple chats can stream in parallel — e.g. user starts chat A,
   *  switches to a new chat B, and sends while A is still running. */
  sendingChatIds: Set<string>;
  /** Set of thinking block IDs that are expanded */
  expandedThinking: Set<string>;
  /** Chat mode: turbo / fast / thinking-medium / thinking-high / thinking-max */
  chatMode: ChatMode;
  /** 关掉「极速模式」时要回到的标准档。极速是个独立开关，用户在它之前挑的思考强度
   *  不该被吞掉——退出极速要回到原来那一档，而不是一律掉回 fast。 */
  lastStandardMode: ThinkingEffort;
  /** 当前对话选中的模式 slug（chat_modes.slug）。standard=标准、turbo=极速，
   *  以及管理员/用户自建的模式。发送时作为 mode_slug 上行，决定这段对话的工具面
   *  与专属提示词；chatMode 只管思考强度，两者正交。 */
  modeSlug: string;
  /** Tool result detail panel state */
  toolResultPanel: {
    key: string;
    toolName: string;
    displayName: string;
    output: unknown;
    summary?: string;
  } | null;
  /** 已复制的那条消息（身份） */
  copiedMsg: string | null;
  /** Whether chats are loading from backend */
  chatsLoading: boolean;
  /** Feedback map: 消息身份 → feedback type */
  feedbackMap: Record<string, 'like' | 'dislike'>;
  /** Message being disliked (for comment modal) */
  dislikingUid: string | null;
  /** Dislike comment text */
  dislikeComment: string;
  /** Tool display names from backend */
  toolDisplayNames: Record<string, string>;
  /** Backend session IDs (tracks which chats exist on the server) */
  backendSessionIds: Set<string>;
  /** Chat IDs whose messages have been loaded from backend */
  loadedMsgIds: Set<string>;
  /** 每个会话的历史分页游标。
   *
   *  打开会话只拉最近一页（后端 order=desc 的第 1 页），用户往上滚到顶再续拉更早的。
   *  过去是 while(true) 把每一页都拉完再一次性渲染 —— 一个跑过大文件的长对话，
   *  光这一下就能把浏览器压垮。`nextPage` 是下一次要取的 desc 页码；`hasOlder`
   *  为 false 表示已经到最早的一条；`loading` 防止滚动抖动触发并发拉取。 */
  messagePaging: Record<string, { nextPage: number; beforeSeq?: number; hasOlder: boolean; loading: boolean }>;
  setMessagePaging: (
    chatId: string,
    paging: { nextPage: number; beforeSeq?: number; hasOlder: boolean; loading: boolean } | null,
  ) => void;
  /** 把按需取回的完整工具结果写回对应的那张工具卡（见 ToolCallRow 的展开取全文）。 */
  applyToolCallOutput: (
    chatId: string,
    messageId: string,
    toolId: string,
    output: unknown,
  ) => void;
  /** Whether share selection mode is enabled */
  shareSelectionMode: boolean;
  /** Selected message identities for share generation */
  selectedShareMessageUids: Set<string>;
  /** Message timestamp to scroll into view after jumping from share records */
  pendingScrollMessageTs: number | null;
  /** Whether plan mode is enabled */
  planMode: boolean;
  /** Whether autonomous-loop mode is enabled */
  loopMode: boolean;
  /** Current plan ID being executed in plan mode */
  currentPlanId: string | null;
  /** 正在编辑的那条用户消息（身份） */
  editingMessageUid: string | null;
  /** Monotonic counter incremented after each fetchSessions completes;
   *  used as an effect dependency to re-trigger the lazy message loader. */
  sessionLoadEpoch: number;
  /** Map of chatId → currently active backend run (set when a run is launched
   *  or discovered via /v1/chats/{chat_id}/active-run on resume).
   *  This is what the stop button cancels. Decoupled from `sendingChatIds`
   *  (which only reflects the current tab's local SSE connection). */
  activeRuns: Record<string, { runId: string; messageId: string; lastOffset?: number }>;
  /** One unsent/steering composer message per chat. Kept out of persisted chat
   *  history until it is actually sent or acknowledged by the running agent. */
  queuedMessages: Record<string, QueuedChatMessage>;
  /** chatId → whether a compaction_notice event was received. After the previous turn ended the
   *  backend compacted earlier context; it notifies once on this turn's first frame. The UI shows
   *  a dismissible banner (not persisted). */
  compactionNotices: Record<string, boolean>;
  /** 正在识图的会话 → 图片张数。视觉桥在模型开口前先把图转成文字，这几秒里轮级状态
   *  显示「图像理解中」而不是笼统的「深度拥抱中」。识图结束即清空。 */
  visionReading: Record<string, number>;
  /** chatId → latest server checkpoint baseline used by ContextGauge. */
  contextCompactions: Record<string, ContextCompactionState>;
  /** chatId → latest provider measurement or explicit backend fallback. */
  contextUsages: Record<string, ContextUsageSnapshot>;
  /** chatId → live plan/progress shown by the plan bar above the input (transient, not persisted).
   *  Fed by the agent's update_plan tool (plan_update SSE) and by manual plan-mode execution. */
  planProgress: Record<string, PlanProgressState | null>;

  // ── Actions ──
  setStore: (store: ChatStoreData) => void;
  updateStore: (updater: (prev: ChatStoreData) => ChatStoreData) => void;
  /** 切到某段会话，并把地址栏推到 `/c/<会话id>`。 */
  setCurrentChatId: (id: string) => void;
  /** 同上，但不改地址栏——供路由把「地址 → 状态」这一方向同步回来（前进 / 后退）。 */
  adoptChatFromUrl: (id: string) => void;
  syncCurrentChatMode: () => void;
  /** First message pending send across panels (project-page input box → chat panel auto-send).
   *  Once set, an effect in App.tsx consumes it when currentChatId matches, then clears it. */
  pendingFirstMessage: { chatId: string; content: string } | null;
  setPendingFirstMessage: (p: { chatId: string; content: string } | null) => void;
  setSending: (v: boolean) => void;
  /** Mark a chat id as currently streaming. Adds to set + updates derived `sending`. */
  addSendingChatId: (id: string) => void;
  /** Mark a chat id as no longer streaming. Removes from set + updates derived `sending`. */
  removeSendingChatId: (id: string) => void;
  /** 在别处（另一台设备 / 另一个标签页）还在跑的会话。
   *
   *  `sendingChatIds` 只记本标签页自己挂着的流，所以换设备登录时侧边栏对没点开过的
   *  会话一无所知。这里存服务端快照，登录、拉完会话列表、窗口切回来时刷新，让
   *  「运行中」小圆点不必等用户点进去才亮。 */
  remoteRunningChatIds: Set<string>;
  /** 重新问一次服务端"我还有哪些会话在跑"。失败不改现状：宁可灯保持上一次的样子，
   *  也不要因为一次网络抖动把正在跑的灯全灭掉。 */
  refreshRemoteRunningChats: () => Promise<void>;
  toggleThinking: (id: string) => void;
  setChatMode: (v: ChatMode) => void;
  setModeSlug: (v: string) => void;
  setToolResultPanel: (panel: ChatState['toolResultPanel']) => void;
  setCopiedMsg: (uid: string | null) => void;
  setChatsLoading: (v: boolean) => void;
  setFeedbackMap: (map: Record<string, 'like' | 'dislike'>) => void;
  setDislikingUid: (uid: string | null) => void;
  setDislikeComment: (comment: string) => void;
  setToolDisplayNames: (names: Record<string, string>) => void;
  addBackendSessionId: (id: string) => void;
  removeBackendSessionId: (id: string) => void;
  clearBackendSessionIds: () => void;
  addLoadedMsgId: (id: string) => void;
  removeLoadedMsgId: (id: string) => void;
  clearLoadedMsgIds: () => void;
  setShareSelectionMode: (v: boolean) => void;
  toggleShareMessageUid: (uid: string) => void;
  clearShareSelection: () => void;
  /** Enter "share selection" mode with the given message identities pre-checked */
  startShareSelectionWithAll: (uidList: string[]) => void;
  setPendingScrollMessageTs: (ts: number | null) => void;
  setPlanMode: (v: boolean) => void;
  setLoopMode: (v: boolean) => void;
  setCurrentPlanId: (id: string | null) => void;
  /** Update/clear the plan bar state for a chat (null clears it) */
  setPlanProgress: (chatId: string, p: PlanProgressState | null) => void;
  /** Enter plan / batch-execution mode (shared by the composer "+" menu and the app center).
   *  Plan and batch are mutually exclusive.
   *  - `opts.inPlace` (composer "+" menu): always switch the **current chat** to that mode in
   *    place — no new chat, no navigation — avoiding "the whole chat jumping back to home".
   *  - Default (app center): reuse the current chat in place if it's empty, otherwise create a
   *    new chat in that mode. */
  enterChatMode: (mode: 'plan' | 'batch' | 'workflow', opts?: { inPlace?: boolean }) => void;
  /** Leave plan / batch mode on the current chat and continue as an ordinary conversation.
   *  The historical planChat / batchChat classification is kept (plan cards and batch history
   *  stay recognisable); only the composer/routing flag is turned off. */
  exitChatMode: (mode: 'plan' | 'batch' | 'workflow') => void;
  /** Enter "site building" mode (Lab → Sites): create a new siteChat session and switch to the
   *  main chat, fully reusing the main-chat composer (attachments / projects / "+" menu).
   *  Mutually exclusive with plan / batch. */
  /** Enter a site-building chat; automatically sets the installed "sites" plugin as activePlugin
   *  (injecting the site-builder skill + site_publish tool). Returns whether the plugin is
   *  installed — when not installed the caller should guide the user to the plugin marketplace.
   *  When opts.projectId is given, binds the chat to that site source-code project (both building
   *  and editing happen inside the project folder; messages are sent with project_id
   *  automatically); opts.title is used as the chat/project display name. */
  enterSiteMode: (opts?: { projectId?: string; projectName?: string; title?: string }) => boolean;
  setEditingMessageUid: (uid: string | null) => void;
  bumpSessionLoadEpoch: () => void;
  /** Truncate messages from the given message (inclusive), located by its identity */
  truncateMessagesFrom: (chatId: string, anchor: Pick<ChatMessage, 'uid'>) => void;
  setActiveRun: (chatId: string, info: { runId: string; messageId: string; lastOffset?: number }) => void;
  clearActiveRun: (chatId: string) => void;
  setQueuedMessage: (chatId: string, queued: QueuedChatMessage | null) => void;
  updateQueuedMessage: (
    chatId: string,
    updater: (queued: QueuedChatMessage) => QueuedChatMessage,
  ) => void;
  /** Mark that a chat received a compaction notice (SSE compaction_notice event) */
  setCompactionNotice: (chatId: string) => void;
  setVisionReading: (chatId: string, count: number) => void;
  /** User dismissed the compaction banner */
  dismissCompactionNotice: (chatId: string) => void;
  /** Replace or clear the active compacted-context baseline for a chat. */
  setContextCompaction: (chatId: string, state: ContextCompactionState | null) => void;
  /** Replace or clear the authoritative context-usage snapshot for a chat. */
  setContextUsage: (chatId: string, state: ContextUsageSnapshot | null) => void;

  /** Bind a chat to a project (Claude-style workspace). Creates the chat entry on demand if it
   *  doesn't exist, making chat.projectId the single source of truth — the next message is sent
   *  with project_id automatically. */
  bindChatProject: (chatId: string, projectId: string, projectName: string) => void;
  /** 混合架构：设置对话运行位置（'local' 本机 / undefined 云端）。仅未开聊的对话可改。 */
  setChatRunTarget: (chatId: string, target: 'local' | undefined) => void;
  /** Unbind a chat from its project. */
  unbindChatProject: (chatId: string) => void;

  /** Create a new chat and switch to it */
  homeDraftId: string | null;
  resumeHomeChat: () => void;
  newChat: () => void;
  /** Delete a chat by id */
  deleteChat: (id: string) => void;
  /** Update messages for a given chat */
  updateMessages: (chatId: string, messages: ChatMessage[]) => void;
  /** Get the current chat item */
  currentChat: () => ChatItem | undefined;
  /** Load chat data from localStorage scoped to the given user id and switch
   *  the store into that user's context. Idempotent — calling twice with the
   *  same id is a no-op. */
  hydrateForUser: (userId: string) => void;
  /** Detach from the current user: clear in-memory chat state so the next
   *  user's data is never visually mixed in. The user's own per-user keys
   *  are intentionally left in localStorage so they resume on next login. */
  clearForLogout: () => void;
}
