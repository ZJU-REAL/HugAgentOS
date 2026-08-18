import { create } from 'zustand';
import type { ChatItem, ChatMessage, ChatStore as ChatStoreData, ContextCompactionState, PlanProgressState } from '../types';
import { loadChatStore, saveChatStoreDebounced, flushChatStore, nowId, userScopedKey, purgeLegacyUnscopedKeys, mergeChatStores, registerDeletedChatId, setStreamingIdsProvider, STORAGE_KEY } from '../storage';
import { usePageConfigStore } from './pageConfigStore';
import { usePluginStore } from './pluginStore';
import { t } from '../i18n';
import { resolveModeSlug, resolvePlanModeActive } from '../utils/chatMode';

/** Fixed slug of the site-building plugin (plugin_bundles/marketplace/sites). Site-building
 *  capability (publish_site tool + site-builder guidance skill) is provided by it — removed from
 *  global defaults, now purely plugin-gated (available only when installed + selected). */
export const SITES_PLUGIN_SLUG = 'sites';

export type ChatMode = 'turbo' | 'fast' | 'medium' | 'high' | 'max';

export interface QueuedChatMessage {
  id: string;
  content: string;
  createdAt: number;
  status: 'queued' | 'steering' | 'applied';
  appliedMessageId?: string;
}

const VALID_CHAT_MODES: readonly ChatMode[] = ['turbo', 'fast', 'medium', 'high', 'max'];

/** Read the admin-configured "default chat mode". Prefer the chat_mode field; fall back to
 *  thinking_mode when unrecognized; if neither is set → fast. */
function adminDefaultChatMode(): ChatMode {
  const defaults = usePageConfigStore.getState().config.defaults;
  const raw = defaults?.chat_mode as string | undefined;
  if (raw && (VALID_CHAT_MODES as readonly string[]).includes(raw)) {
    return raw as ChatMode;
  }
  // Fallback for the legacy field
  return defaults?.thinking_mode ? 'medium' : 'fast';
}

/** 重置对话时同时把「极速开关」和「思考强度」拉回管理端默认——只写 chatMode 会让
 *  lastStandardMode 留着上一段对话的档位，退出极速时跳回一个用户没选过的强度。 */
function applyDefaultChatMode(): { chatMode: ChatMode; lastStandardMode: ThinkingEffort } {
  const mode = adminDefaultChatMode();
  return { chatMode: mode, lastStandardMode: mode === 'turbo' ? 'fast' : mode };
}

/** chatMode → whether thinking mode is active (used as the equivalent for hooks/UI toggle buttons). */
export function isThinkingMode(mode: ChatMode): boolean {
  return mode !== 'fast' && mode !== 'turbo';
}

/** 「标准模式」下可选的思考强度档位；chatMode 除 turbo 外的取值就是它。 */
export type ThinkingEffort = Exclude<ChatMode, 'turbo'>;

/** 极速模式（turbo）与标准模式互斥：UI 把这一位拆成对话框上方的独立开关，
 *  思考强度则挂到右侧模型位上，但落到线上仍是同一个 chat_mode 字段。 */
export function isTurboMode(mode: ChatMode): boolean {
  return mode === 'turbo';
}

const CURRENT_CHAT_KEY = 'hugagent_current_chat_id';
const PENDING_SCROLL_MESSAGE_TS_KEY = 'hugagent_pending_scroll_message_ts';

function loadCurrentChatId(userId: string | null | undefined) {
  if (typeof window === 'undefined') return nowId('chat');
  const key = userScopedKey(CURRENT_CHAT_KEY, userId);
  if (!key) return nowId('chat');
  // 标签页私有优先：sessionStorage 与本标签页同生共死（浏览器"复制标签页"会带
  // 一份副本，恰好落在同一会话上）。多个标签页各自恢复自己的会话，不再共抢
  // localStorage 里的单一指针互相覆盖（问题17 串台的一环）；localStorage 只作为
  // 新开标签页的兜底。
  try {
    const tabLocal = window.sessionStorage.getItem(key);
    if (tabLocal) return tabLocal;
  } catch { /* sessionStorage 不可用时退回 localStorage */ }
  return window.localStorage.getItem(key) || nowId('chat');
}

function saveCurrentChatId(userId: string | null | undefined, chatId: string) {
  if (typeof window === 'undefined') return;
  const key = userScopedKey(CURRENT_CHAT_KEY, userId);
  if (!key) return;
  try { window.sessionStorage.setItem(key, chatId); } catch { /* ignore */ }
  window.localStorage.setItem(key, chatId);
}

function loadPendingScrollMessageTs(userId: string | null | undefined) {
  if (typeof window === 'undefined') return null;
  const key = userScopedKey(PENDING_SCROLL_MESSAGE_TS_KEY, userId);
  if (!key) return null;
  const raw = window.localStorage.getItem(key);
  if (!raw) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

function savePendingScrollMessageTs(userId: string | null | undefined, ts: number | null) {
  if (typeof window === 'undefined') return;
  const key = userScopedKey(PENDING_SCROLL_MESSAGE_TS_KEY, userId);
  if (!key) return;
  if (ts === null) {
    window.localStorage.removeItem(key);
    return;
  }
  window.localStorage.setItem(key, String(ts));
}

const QUEUED_MESSAGES_KEY = 'hugagent_queued_messages';

/** 排队消息持久化：**只存 status==='queued' 的**。steering/applied 的指令已经交给
 *  后端 run（steerChatRun），刷新后恢复成待发送会重复下发；queued 的恢复成待发送
 *  卡片即可——有续播 run 就等它结束自动接管，没有就留给用户点发送/删除。 */
function loadQueuedMessages(userId: string | null | undefined): Record<string, QueuedChatMessage> {
  if (typeof window === 'undefined') return {};
  const key = userScopedKey(QUEUED_MESSAGES_KEY, userId);
  if (!key) return {};
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, QueuedChatMessage>;
    const out: Record<string, QueuedChatMessage> = {};
    for (const [chatId, q] of Object.entries(parsed || {})) {
      if (q && q.status === 'queued' && typeof q.content === 'string' && q.content) out[chatId] = q;
    }
    return out;
  } catch {
    return {};
  }
}

function saveQueuedMessages(
  userId: string | null | undefined,
  map: Record<string, QueuedChatMessage>,
) {
  if (typeof window === 'undefined') return;
  const key = userScopedKey(QUEUED_MESSAGES_KEY, userId);
  if (!key) return;
  const persistable = Object.entries(map).filter(([, q]) => q?.status === 'queued');
  try {
    if (persistable.length === 0) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, JSON.stringify(Object.fromEntries(persistable)));
  } catch { /* localStorage 不可用时放弃持久化，不影响内存态 */ }
}

/** 恢复对话记录上的思考强度档。没记录（老数据 / 没显式选过）返回 {}——沿用当前
 *  会话档位，避免切一次对话就把用户刚选的档位吞掉。 */
function restoredEffort(
  chat?: ChatItem,
): Partial<{ chatMode: ChatMode; lastStandardMode: ThinkingEffort }> {
  const saved = chat?.thinkingEffort;
  if (!saved || !(VALID_CHAT_MODES as readonly string[]).includes(saved)) return {};
  return saved === 'turbo'
    ? { chatMode: saved }
    : { chatMode: saved, lastStandardMode: saved };
}

interface ChatState {
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
  /** Input text */
  input: string;
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
  /** Copied message index */
  copiedMsg: number | null;
  /** Whether chats are loading from backend */
  chatsLoading: boolean;
  /** Feedback map: message timestamp → feedback type */
  feedbackMap: Record<number, 'like' | 'dislike'>;
  /** Message being disliked (for comment modal) */
  dislikingTs: number | null;
  /** Dislike comment text */
  dislikeComment: string;
  /** Tool display names from backend */
  toolDisplayNames: Record<string, string>;
  /** Backend session IDs (tracks which chats exist on the server) */
  backendSessionIds: Set<string>;
  /** Chat IDs whose messages have been loaded from backend */
  loadedMsgIds: Set<string>;
  /** Whether share selection mode is enabled */
  shareSelectionMode: boolean;
  /** Selected message timestamps for share generation */
  selectedShareMessageTs: Set<number>;
  /** Message timestamp to scroll into view after jumping from share records */
  pendingScrollMessageTs: number | null;
  /** Quoted message used for follow-up prompting */
  quotedFollowUp: { text: string; ts: number } | null;
  /** Active skill selected via / slash command */
  activeSkill: { id: string; name: string } | null;
  /** Active plugin referenced via / or + menu. On send: skillIds→skill_ids (injects skill
   *  instructions), mcpIds→mcp_ids (force-enables the plugin's MCP tools into this turn's toolset). */
  activePlugin: { name: string; skillIds: string[]; mcpIds: string[] } | null;
  /** Active @mention selected via popup; id is the authoritative per-turn direct target. */
  activeMention: { id: string; name: string } | null;
  /** Whether plan mode is enabled */
  planMode: boolean;
  /** Whether autonomous-loop mode is enabled */
  loopMode: boolean;
  /** Current plan ID being executed in plan mode */
  currentPlanId: string | null;
  /** Timestamp of user message being edited */
  editingMessageTs: number | null;
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
  /** chatId → live plan/progress shown by the plan bar above the input (transient, not persisted).
   *  Fed by the agent's update_plan tool (plan_update SSE) and by manual plan-mode execution. */
  planProgress: Record<string, PlanProgressState | null>;

  // ── Actions ──
  setStore: (store: ChatStoreData) => void;
  updateStore: (updater: (prev: ChatStoreData) => ChatStoreData) => void;
  setCurrentChatId: (id: string) => void;
  setInput: (input: string) => void;
  /** First message pending send across panels (project-page input box → chat panel auto-send).
   *  Once set, an effect in App.tsx consumes it when currentChatId matches, then clears it. */
  pendingFirstMessage: { chatId: string; content: string } | null;
  setPendingFirstMessage: (p: { chatId: string; content: string } | null) => void;
  setSending: (v: boolean) => void;
  /** Mark a chat id as currently streaming. Adds to set + updates derived `sending`. */
  addSendingChatId: (id: string) => void;
  /** Mark a chat id as no longer streaming. Removes from set + updates derived `sending`. */
  removeSendingChatId: (id: string) => void;
  toggleThinking: (id: string) => void;
  setChatMode: (v: ChatMode) => void;
  setModeSlug: (v: string) => void;
  setToolResultPanel: (panel: ChatState['toolResultPanel']) => void;
  setCopiedMsg: (ts: number | null) => void;
  setChatsLoading: (v: boolean) => void;
  setFeedbackMap: (map: Record<number, 'like' | 'dislike'>) => void;
  setDislikingTs: (ts: number | null) => void;
  setDislikeComment: (comment: string) => void;
  setToolDisplayNames: (names: Record<string, string>) => void;
  addBackendSessionId: (id: string) => void;
  removeBackendSessionId: (id: string) => void;
  clearBackendSessionIds: () => void;
  addLoadedMsgId: (id: string) => void;
  removeLoadedMsgId: (id: string) => void;
  clearLoadedMsgIds: () => void;
  setShareSelectionMode: (v: boolean) => void;
  toggleShareMessageTs: (ts: number) => void;
  clearShareSelection: () => void;
  /** Enter "share selection" mode with the given message ts list pre-checked */
  startShareSelectionWithAll: (tsList: number[]) => void;
  setPendingScrollMessageTs: (ts: number | null) => void;
  setQuotedFollowUp: (quote: { text: string; ts: number } | null) => void;
  setActiveSkill: (skill: { id: string; name: string } | null) => void;
  setActivePlugin: (plugin: { name: string; skillIds: string[]; mcpIds: string[] } | null) => void;
  setActiveMention: (mention: { id: string; name: string } | null) => void;
  setPlanMode: (v: boolean) => void;
  setLoopMode: (v: boolean) => void;
  /** Sync the "composer context" when switching the main view panel: activePlugin references the
   *  "sites" plugin only on the chat panel + a site-building chat, and is cleared everywhere else
   *  (prevents the sites plugin reference leaking into the projects page/other pages); leaving the
   *  chat panel also turns off autonomous-loop mode. Called by App on panel changes. */
  syncComposerForPanel: (panel: string) => void;
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
  setEditingMessageTs: (ts: number | null) => void;
  bumpSessionLoadEpoch: () => void;
  /** Truncate messages from the given timestamp (inclusive) */
  truncateMessagesFrom: (chatId: string, ts: number) => void;
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

  /** Bind a chat to a project (Claude-style workspace). Creates the chat entry on demand if it
   *  doesn't exist, making chat.projectId the single source of truth — the next message is sent
   *  with project_id automatically. */
  bindChatProject: (chatId: string, projectId: string, projectName: string) => void;
  /** 混合架构：设置对话运行位置（'local' 本机 / undefined 云端）。仅未开聊的对话可改。 */
  setChatRunTarget: (chatId: string, target: 'local' | undefined) => void;
  /** Unbind a chat from its project. */
  unbindChatProject: (chatId: string) => void;

  /** Create a new chat and switch to it */
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

export const useChatStore = create<ChatState>((set, get) => {
  // 合并写盘时：本标签页正在流式输出的会话一律以本内存版本为准
  setStreamingIdsProvider(() => get().sendingChatIds);
  return ({
  // currentUserId stays null until hydrateForUser runs after login. While null,
  // the store is empty and all save helpers no-op — avoids any chance of
  // writing one user's data under a key that a later user could read.
  currentUserId: null,
  store: { chats: {}, order: [] },
  storeRef: { chats: {}, order: [] },
  currentChatId: nowId('chat'),
  input: '',
  sending: false,
  sendingChatIds: new Set(),
  expandedThinking: new Set(),
  chatMode: 'fast',
  lastStandardMode: 'fast',
  modeSlug: 'standard',
  toolResultPanel: null,
  copiedMsg: null,
  chatsLoading: false,
  pendingFirstMessage: null,
  feedbackMap: {},
  dislikingTs: null,
  dislikeComment: '',
  toolDisplayNames: {},
  backendSessionIds: new Set(),
  loadedMsgIds: new Set(),
  shareSelectionMode: false,
  selectedShareMessageTs: new Set(),
  pendingScrollMessageTs: null,
  quotedFollowUp: null,
  activeSkill: null,
  activePlugin: null,
  activeMention: null,
  planMode: false,
  loopMode: false,
  currentPlanId: null,
  editingMessageTs: null,
  sessionLoadEpoch: 0,
  activeRuns: {},
  queuedMessages: {},
  compactionNotices: {},
  visionReading: {},
  contextCompactions: {},
  planProgress: {},

  setStore: (store) => {
    set({ store, storeRef: store });
    saveChatStoreDebounced(get().currentUserId, store);
  },
  updateStore: (updater) => {
    const next = updater(get().store);
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(get().currentUserId, next);
  },
  setCurrentChatId: (id) => {
    saveCurrentChatId(get().currentUserId, id);
    const chat = get().store.chats[id];
    // activePlugin is global state but semantically belongs to the "current chat". On chat switch,
    // recompute it for the target chat: only site-building chats (siteChat) reference the "sites"
    // plugin by default; all other chats clear it, so the sites plugin reference doesn't linger
    // into other conversations.
    let nextActivePlugin: ChatState['activePlugin'] = null;
    if (chat?.siteChat) {
      const sitesPlugin = usePluginStore
        .getState()
        .installed.find((p) => p.slug === SITES_PLUGIN_SLUG && p.enabled !== false);
      if (sitesPlugin) {
        nextActivePlugin = {
          name: sitesPlugin.name,
          skillIds: sitesPlugin.skills || [],
          mcpIds: sitesPlugin.mcp || [],
        };
      }
    }
    set({
      currentChatId: id,
      sending: get().sendingChatIds.has(id),
      planMode: resolvePlanModeActive(chat),
      // 模式跟着对话走：切到哪段对话就恢复它记录上的模式，没记录的按标准模式。
      modeSlug: resolveModeSlug(chat),
      // 思考强度与待发送引用同理随对话记录恢复；强度没记录时沿用当前档位。
      ...restoredEffort(chat),
      quotedFollowUp: chat?.pendingQuote || null,
      // Autonomous loop is a "one-shot composer intent" and does not persist with the chat —
      // switching to any chat resets to a normal conversation, so a loop mode left on in the
      // previous chat doesn't carry over into a new/other chat.
      loopMode: false,
      currentPlanId: null,
      activePlugin: nextActivePlugin,
      activeMention: null,
    });
  },
  setInput: (input) => set({ input }),
  setSending: (v) => set({ sending: v }),
  addSendingChatId: (id) => set((s) => {
    const next = new Set(s.sendingChatIds);
    next.add(id);
    return { sendingChatIds: next, sending: next.has(s.currentChatId) };
  }),
  removeSendingChatId: (id) => set((s) => {
    const next = new Set(s.sendingChatIds);
    next.delete(id);
    return { sendingChatIds: next, sending: next.has(s.currentChatId) };
  }),
  toggleThinking: (id) => {
    const next = new Set(get().expandedThinking);
    if (next.has(id)) next.delete(id); else next.add(id);
    set({ expandedThinking: next });
  },
  setChatMode: (v) => {
    const ui = v === 'turbo'
      ? { chatMode: v }
      : { chatMode: v, lastStandardMode: v as ThinkingEffort };
    const { currentChatId, currentUserId, store } = get();
    const chat = store.chats[currentChatId];
    if (!chat) {
      // 记录还没建（首条消息前）：先记 UI 态，useStreaming 建记录时补落。
      set(ui);
      return;
    }
    const next: ChatStoreData = {
      ...store,
      chats: { ...store.chats, [currentChatId]: { ...chat, thinkingEffort: v } },
    };
    set({ ...ui, store: next, storeRef: next });
    saveChatStoreDebounced(currentUserId, next);
  },
  setModeSlug: (v) => {
    const slug = v || 'standard';
    const { currentChatId, currentUserId, store } = get();
    const chat = store.chats[currentChatId];
    if (!chat) {
      // 对话记录还没建（首条消息前）：先记 UI 态，useStreaming 建记录时会把
      // 当前 modeSlug 落进去。
      set({ modeSlug: slug });
      return;
    }
    const next: ChatStoreData = {
      ...store,
      chats: { ...store.chats, [currentChatId]: { ...chat, modeSlug: slug } },
    };
    set({ modeSlug: slug, store: next, storeRef: next });
    saveChatStoreDebounced(currentUserId, next);
  },
  setToolResultPanel: (panel) => set({ toolResultPanel: panel }),
  setCopiedMsg: (ts) => set({ copiedMsg: ts }),
  setChatsLoading: (v) => set({ chatsLoading: v }),
  setPendingFirstMessage: (p) => set({ pendingFirstMessage: p }),
  setFeedbackMap: (map) => set({ feedbackMap: map }),
  setDislikingTs: (ts) => set({ dislikingTs: ts }),
  setDislikeComment: (comment) => set({ dislikeComment: comment }),
  setToolDisplayNames: (names) => set({ toolDisplayNames: names }),
  addBackendSessionId: (id) => set((s) => {
    const next = new Set(s.backendSessionIds);
    next.add(id);
    return { backendSessionIds: next };
  }),
  removeBackendSessionId: (id) => set((s) => {
    const next = new Set(s.backendSessionIds);
    next.delete(id);
    return { backendSessionIds: next };
  }),
  clearBackendSessionIds: () => set({ backendSessionIds: new Set() }),
  addLoadedMsgId: (id) => set((s) => {
    const next = new Set(s.loadedMsgIds);
    next.add(id);
    return { loadedMsgIds: next };
  }),
  removeLoadedMsgId: (id) => set((s) => {
    const next = new Set(s.loadedMsgIds);
    next.delete(id);
    return { loadedMsgIds: next };
  }),
  clearLoadedMsgIds: () => set({ loadedMsgIds: new Set() }),
  setShareSelectionMode: (v) => set((s) => ({
    shareSelectionMode: v,
    selectedShareMessageTs: v ? s.selectedShareMessageTs : new Set(),
  })),
  toggleShareMessageTs: (ts) => set((s) => {
    const next = new Set(s.selectedShareMessageTs);
    if (next.has(ts)) next.delete(ts); else next.add(ts);
    return { selectedShareMessageTs: next };
  }),
  clearShareSelection: () => set({ shareSelectionMode: false, selectedShareMessageTs: new Set() }),
  startShareSelectionWithAll: (tsList) => set({
    shareSelectionMode: true,
    selectedShareMessageTs: new Set(tsList),
  }),
  setPendingScrollMessageTs: (ts) => {
    savePendingScrollMessageTs(get().currentUserId, ts);
    set({ pendingScrollMessageTs: ts });
  },
  setQuotedFollowUp: (quote) => {
    const { currentChatId, currentUserId, store } = get();
    const chat = store.chats[currentChatId];
    if (!chat) {
      set({ quotedFollowUp: quote });
      return;
    }
    const nextChat: ChatItem = { ...chat };
    if (quote) nextChat.pendingQuote = quote;
    else delete nextChat.pendingQuote;
    const next: ChatStoreData = {
      ...store,
      chats: { ...store.chats, [currentChatId]: nextChat },
    };
    set({ quotedFollowUp: quote, store: next, storeRef: next });
    saveChatStoreDebounced(currentUserId, next);
  },
  setActiveSkill: (skill) => set({ activeSkill: skill }),
  setActivePlugin: (plugin) => set({ activePlugin: plugin }),
  setActiveMention: (mention) => set({ activeMention: mention }),
  setPlanMode: (v) => {
    const { currentChatId, currentUserId, store } = get();
    const chat = store.chats[currentChatId];
    if (!chat) {
      set(v ? { planMode: true, loopMode: false } : { planMode: false });
      return;
    }
    const next: ChatStoreData = {
      ...store,
      chats: {
        ...store.chats,
        [currentChatId]: { ...chat, planModeActive: v },
      },
    };
    set({
      store: next,
      storeRef: next,
      ...(v ? { planMode: true, loopMode: false } : { planMode: false }),
    });
    saveChatStoreDebounced(currentUserId, next);
  },
  setLoopMode: (v) => set(v ? { loopMode: true, planMode: false } : { loopMode: false }),
  syncComposerForPanel: (panel) => {
    const { currentChatId, store } = get();
    const chat = store.chats[currentChatId];
    // activePlugin keeps the "sites" plugin reference only on "chat panel + site chat"; cleared everywhere else.
    let nextActivePlugin: ChatState['activePlugin'] = null;
    if (panel === 'chat' && chat?.siteChat) {
      const sitesPlugin = usePluginStore
        .getState()
        .installed.find((p) => p.slug === SITES_PLUGIN_SLUG && p.enabled !== false);
      if (sitesPlugin) {
        nextActivePlugin = {
          name: sitesPlugin.name,
          skillIds: sitesPlugin.skills || [],
          mcpIds: sitesPlugin.mcp || [],
        };
      }
    }
    set({
      activePlugin: nextActivePlugin,
      activeMention: null,
      // Leaving the chat panel exits autonomous-loop mode (projects/other pages shouldn't carry this intent).
      ...(panel !== 'chat' ? { loopMode: false } : {}),
    });
  },
  setCurrentPlanId: (id) => set({ currentPlanId: id }),
  setPlanProgress: (chatId, p) => set((s) => ({
    planProgress: { ...s.planProgress, [chatId]: p },
  })),
  setEditingMessageTs: (ts) => set({ editingMessageTs: ts }),
  bumpSessionLoadEpoch: () => set((s) => ({ sessionLoadEpoch: s.sessionLoadEpoch + 1 })),
  setActiveRun: (chatId, info) => set((s) => ({
    activeRuns: { ...s.activeRuns, [chatId]: info },
  })),
  clearActiveRun: (chatId) => set((s) => {
    const next = { ...s.activeRuns };
    delete next[chatId];
    return { activeRuns: next };
  }),
  setQueuedMessage: (chatId, queued) => {
    set((s) => {
      const next = { ...s.queuedMessages };
      if (queued) next[chatId] = queued;
      else delete next[chatId];
      return { queuedMessages: next };
    });
    saveQueuedMessages(get().currentUserId, get().queuedMessages);
  },
  updateQueuedMessage: (chatId, updater) => {
    set((s) => {
      const current = s.queuedMessages[chatId];
      if (!current) return { queuedMessages: s.queuedMessages };
      return { queuedMessages: { ...s.queuedMessages, [chatId]: updater(current) } };
    });
    saveQueuedMessages(get().currentUserId, get().queuedMessages);
  },
  setVisionReading: (chatId, count) => set((s) => {
    const next = { ...s.visionReading };
    // count<=0 表示识图结束；留着空条目会让状态一直卡在「图像理解中」
    if (count > 0) next[chatId] = count;
    else delete next[chatId];
    return { visionReading: next };
  }),

  setCompactionNotice: (chatId) => set((s) => ({
    compactionNotices: { ...s.compactionNotices, [chatId]: true },
  })),
  dismissCompactionNotice: (chatId) => set((s) => {
    const next = { ...s.compactionNotices };
    delete next[chatId];
    return { compactionNotices: next };
  }),
  setContextCompaction: (chatId, state) => set((s) => {
    const next = { ...s.contextCompactions };
    if (state) next[chatId] = state;
    else delete next[chatId];
    return { contextCompactions: next };
  }),

  truncateMessagesFrom: (chatId, ts) => {
    const { store } = get();
    const chat = store.chats[chatId];
    if (!chat) return;
    const filtered = chat.messages.filter((m) => m.ts < ts);
    const next: ChatStoreData = {
      ...store,
      chats: { ...store.chats, [chatId]: { ...chat, messages: filtered, updatedAt: Date.now() } },
    };
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(get().currentUserId, next);
  },

  bindChatProject: (chatId, projectId, projectName) => {
    const { store } = get();
    const existing = store.chats[chatId];
    const now = Date.now();
    const nextChat: ChatItem = existing
      ? { ...existing, projectId, projectName, updatedAt: now }
      : {
          id: chatId,
          title: t('新对话'),
          createdAt: now,
          updatedAt: now,
          messages: [],
          favorite: false,
          pinned: false,
          businessTopic: '综合咨询',
          projectId,
          projectName,
        };
    const next: ChatStoreData = {
      chats: { ...store.chats, [chatId]: nextChat },
      // Don't add to order proactively: a newly created empty chat doesn't enter the sidebar
      // history until its first message is sent (useStreaming writes it into order on send);
      // existing chats keep their original position.
      order: store.order,
    };
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(get().currentUserId, next);
  },

  setChatRunTarget: (chatId, target) => {
    const { store } = get();
    const existing = store.chats[chatId];
    const now = Date.now();
    const base: ChatItem = existing ?? {
      id: chatId,
      title: t('新对话'),
      createdAt: now,
      updatedAt: now,
      messages: [],
      favorite: false,
      pinned: false,
      businessTopic: '综合咨询',
    };
    const nextChat: ChatItem = { ...base, updatedAt: now };
    if (target === 'local') nextChat.runTarget = 'local';
    else delete nextChat.runTarget;
    const next: ChatStoreData = {
      chats: { ...store.chats, [chatId]: nextChat },
      order: store.order,
    };
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(get().currentUserId, next);
  },

  unbindChatProject: (chatId) => {
    const { store } = get();
    const existing = store.chats[chatId];
    if (!existing) return;
    const nextChat: ChatItem = { ...existing, updatedAt: Date.now() };
    delete nextChat.projectId;
    delete nextChat.projectName;
    const next: ChatStoreData = {
      ...store,
      chats: { ...store.chats, [chatId]: nextChat },
    };
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(get().currentUserId, next);
  },

  enterChatMode: (mode, opts) => {
    const inPlace = !!opts?.inPlace;
    const { store, currentChatId, currentUserId, sendingChatIds } = get();
    const planChat = mode === 'plan';
    const existing = store.chats[currentChatId];
    const now = Date.now();
    // inPlace: always switch the current chat in place (no new chat, no navigation).
    // Otherwise: current chat has no messages yet → reuse in place; has messages → create a new chat in that mode.
    const reuse = inPlace || !existing || existing.messages.length === 0;
    const targetId = reuse ? currentChatId : nowId('chat');
    const base: ChatItem = reuse && existing
      ? existing
      : {
          id: targetId,
          title: t('新对话'),
          createdAt: now,
          updatedAt: now,
          messages: [],
          favorite: false,
          pinned: false,
          businessTopic: '综合咨询',
          // New chats inherit the source chat's project binding, keeping plan/batch under the current project
          ...(existing?.projectId
            ? { projectId: existing.projectId, projectName: existing.projectName }
            : {}),
        };
    const nextChat: ChatItem = { ...base, id: targetId, updatedAt: now };
    // Plan / batch / workflow 三者互斥：进入一个就清掉另外两个的标记
    delete nextChat.planChat;
    delete nextChat.planModeActive;
    delete nextChat.batchChat;
    delete nextChat.batchModeActive;
    delete nextChat.workflowChat;
    delete nextChat.workflowModeActive;
    if (mode === 'plan') {
      nextChat.planChat = true;
      nextChat.planModeActive = true;
    } else if (mode === 'workflow') {
      nextChat.workflowChat = true;
      nextChat.workflowModeActive = true;
    } else {
      nextChat.batchChat = true;
      nextChat.batchModeActive = true;
    }
    const next: ChatStoreData = {
      chats: { ...store.chats, [targetId]: nextChat },
      // When reusing an empty chat, leave order untouched (it enters the sidebar history when the first message is sent, consistent with bindChatProject); new chats go to the top.
      order: reuse ? store.order : [targetId, ...store.order.filter((oid) => oid !== targetId)],
    };
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(currentUserId, next);
    saveCurrentChatId(currentUserId, targetId);
    set({
      currentChatId: targetId,
      planMode: planChat,
      // Plan / batch and the autonomous loop are mutually exclusive: leaving loopMode on would
      // keep a stale composer intent that resurfaces the moment plan/batch is closed again.
      loopMode: false,
      currentPlanId: null,
      toolResultPanel: null,
      quotedFollowUp: nextChat.pendingQuote || null,
      sending: sendingChatIds.has(targetId),
    });
  },

  exitChatMode: (mode) => {
    if (mode === 'plan') {
      // setPlanMode already persists planModeActive = false on the current chat.
      get().setPlanMode(false);
      return;
    }
    const { currentChatId, currentUserId, store } = get();
    const chat = store.chats[currentChatId];
    if (!chat) return;
    const patch =
      mode === 'workflow' ? { workflowModeActive: false } : { batchModeActive: false };
    const next: ChatStoreData = {
      ...store,
      chats: {
        ...store.chats,
        [currentChatId]: { ...chat, ...patch },
      },
    };
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(currentUserId, next);
  },

  enterSiteMode: (opts) => {
    const { store, currentChatId, currentUserId, sendingChatIds } = get();
    // Resolve the installed "sites" plugin: site-building capability is purely plugin-gated; only when installed do its skills + MCP get attached to this chat.
    const sitesPlugin = usePluginStore
      .getState()
      .installed.find((p) => p.slug === SITES_PLUGIN_SLUG && p.enabled !== false);
    const sitesActivePlugin = sitesPlugin
      ? { name: sitesPlugin.name, skillIds: sitesPlugin.skills || [], mcpIds: sitesPlugin.mcp || [] }
      : null;
    const projectId = opts?.projectId?.trim() || undefined;
    // When editing an existing site, name the chat after the site title and avoid reusing the current empty chat (switch to a clean editing chat).
    const isEdit = !!(projectId && opts?.title);
    const existing = store.chats[currentChatId];
    const now = Date.now();
    // If the current chat is empty and this isn't an edit, reuse in place; otherwise create a new site-building chat (consistent with enterChatMode).
    const reuse = !isEdit && (!existing || existing.messages.length === 0);
    const targetId = reuse ? currentChatId : nowId('chat');
    const base: ChatItem = reuse && existing
      ? existing
      : {
          id: targetId,
          title: opts?.title ? t('编辑站点：{name}', { name: opts.title }) : t('新对话'),
          createdAt: now,
          updatedAt: now,
          messages: [],
          favorite: false,
          pinned: false,
          businessTopic: '综合咨询',
        };
    // Site building is mutually exclusive with plan / batch
    const nextChat: ChatItem = { ...base, id: targetId, updatedAt: now, siteChat: true };
    delete nextChat.planChat;
    delete nextChat.planModeActive;
    delete nextChat.batchChat;
    delete nextChat.batchModeActive;
    // Bind the site source-code project → messages are sent with project_id automatically, and agent file tools operate inside the project folder.
    if (projectId) {
      nextChat.projectId = projectId;
      if (opts?.projectName) nextChat.projectName = opts.projectName;
    } else {
      delete nextChat.projectId;
      delete nextChat.projectName;
    }
    const next: ChatStoreData = {
      chats: { ...store.chats, [targetId]: nextChat },
      order: reuse ? store.order : [targetId, ...store.order.filter((oid) => oid !== targetId)],
    };
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(currentUserId, next);
    saveCurrentChatId(currentUserId, targetId);
    set({
      currentChatId: targetId,
      planMode: false,
      currentPlanId: null,
      toolResultPanel: null,
      input: '',
      quotedFollowUp: nextChat.pendingQuote || null,
      activeSkill: null,
      // "Sites" plugin installed → activate it automatically (site-builder skill + site_publish tool delivered with this turn).
      activePlugin: sitesActivePlugin,
      activeMention: null,
      loopMode: false,
      sending: sendingChatIds.has(targetId),
    });
    return !!sitesPlugin;
  },

  newChat: () => {
    const id = nowId('chat');
    saveCurrentChatId(get().currentUserId, id);
    set({
      currentChatId: id,
      input: '',
      sending: false,
      expandedThinking: new Set(),
      shareSelectionMode: false,
      selectedShareMessageTs: new Set(),
      quotedFollowUp: null,
      activeSkill: null,
      activePlugin: null,
      activeMention: null,
      planMode: false,
      currentPlanId: null,
      loopMode: false,
      ...applyDefaultChatMode(),
      modeSlug: 'standard',
    });
  },

  deleteChat: (id) => {
    const { store, currentChatId, currentUserId } = get();
    registerDeletedChatId(id);
    const rest = { ...store.chats };
    delete rest[id];
    const nextCompactions = { ...get().contextCompactions };
    const nextNotices = { ...get().compactionNotices };
    const nextQueued = { ...get().queuedMessages };
    delete nextCompactions[id];
    delete nextNotices[id];
    delete nextQueued[id];
    const next: ChatStoreData = {
      chats: rest,
      order: store.order.filter((oid) => oid !== id),
    };
    set({
      store: next,
      storeRef: next,
      contextCompactions: nextCompactions,
      compactionNotices: nextNotices,
      queuedMessages: nextQueued,
    });
    saveChatStoreDebounced(currentUserId, next);
    saveQueuedMessages(currentUserId, nextQueued);
    if (currentChatId === id) {
      const newId = next.order[0] || nowId('chat');
      const nextChat = next.chats[newId];
      saveCurrentChatId(currentUserId, newId);
      set({
        currentChatId: newId,
        planMode: resolvePlanModeActive(nextChat),
        modeSlug: resolveModeSlug(nextChat),
        ...restoredEffort(nextChat),
        loopMode: false,
        currentPlanId: null,
        shareSelectionMode: false,
        selectedShareMessageTs: new Set(),
        quotedFollowUp: nextChat?.pendingQuote || null,
        activeSkill: null,
        activePlugin: null,
        activeMention: null,
      });
    }
  },

  updateMessages: (chatId, messages) => {
    const { store } = get();
    const chat = store.chats[chatId];
    if (!chat) return;
    const next: ChatStoreData = {
      ...store,
      chats: { ...store.chats, [chatId]: { ...chat, messages } },
    };
    set({ store: next, storeRef: next });
    saveChatStoreDebounced(get().currentUserId, next);
  },

  currentChat: () => {
    const { store, currentChatId } = get();
    return store.chats[currentChatId];
  },

  hydrateForUser: (userId) => {
    if (get().currentUserId === userId) return;
    // Flush any pending debounced writes for the previous user before we
    // swap context — otherwise the next user's hydrate could race the queued
    // setItem and overwrite their freshly loaded state.
    flushChatStore();
    // First hydration after the upgrade: drop any pre-userscoped global keys
    // so they can't be observed by anyone after this point.
    purgeLegacyUnscopedKeys();
    const store = loadChatStore(userId);
    const currentChatId = loadCurrentChatId(userId);
    const pendingScroll = loadPendingScrollMessageTs(userId);
    set({
      currentUserId: userId,
      store,
      storeRef: store,
      currentChatId,
      pendingScrollMessageTs: pendingScroll,
      // Reset any in-flight UI state carried over from a previous user.
      sendingChatIds: new Set(),
      backendSessionIds: new Set(),
      loadedMsgIds: new Set(),
      shareSelectionMode: false,
      selectedShareMessageTs: new Set(),
      quotedFollowUp: store.chats[currentChatId]?.pendingQuote || null,
      activeSkill: null,
      activePlugin: null,
      activeMention: null,
      planMode: resolvePlanModeActive(store.chats[currentChatId]),
      loopMode: false,
      currentPlanId: null,
      editingMessageTs: null,
      activeRuns: {},
      // 只恢复 queued 状态的排队消息；steering/applied 的已交给后端 run，不重放。
      queuedMessages: loadQueuedMessages(userId),
      compactionNotices: {},
      contextCompactions: {},
      ...applyDefaultChatMode(),
      // 刷新/重新进入时恢复当前对话记录上的模式与思考强度，不再一律掉回默认。
      modeSlug: resolveModeSlug(store.chats[currentChatId]),
      ...restoredEffort(store.chats[currentChatId]),
    });
  },

  clearForLogout: () => {
    // Persist any debounced writes before tearing down — the user's most
    // recent messages must hit disk so they resume correctly on next login.
    flushChatStore();
    // Per-user keys stay on disk so the same user can resume later. We only
    // wipe in-memory state here so the new user (or login screen) never sees
    // the previous user's chats.
    set({
      currentUserId: null,
      store: { chats: {}, order: [] },
      storeRef: { chats: {}, order: [] },
      currentChatId: nowId('chat'),
      input: '',
      sending: false,
      sendingChatIds: new Set(),
      backendSessionIds: new Set(),
      loadedMsgIds: new Set(),
      shareSelectionMode: false,
      selectedShareMessageTs: new Set(),
      pendingScrollMessageTs: null,
      quotedFollowUp: null,
      activeSkill: null,
      activePlugin: null,
      activeMention: null,
      planMode: false,
      loopMode: false,
      currentPlanId: null,
      editingMessageTs: null,
      activeRuns: {},
      queuedMessages: {},
      compactionNotices: {},
      contextCompactions: {},
    });
  },
});
});

// ── 跨标签页同步：另一个标签页写入会话数据时，把它按会话粒度合并进本页内存 ──
// storage 事件只在"其他"标签页触发（写入方不触发），不会自激振荡；合并结果
// 不回写磁盘（磁盘上已是并集），避免写风暴。本页正在流式输出的会话保留本页版本。
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e: StorageEvent) => {
    const st = useChatStore.getState();
    const uid = st.currentUserId;
    if (!uid || !e.newValue) return;
    if (e.key !== userScopedKey(STORAGE_KEY, uid)) return;
    let incoming: ChatStoreData | null = null;
    try {
      const parsed = JSON.parse(e.newValue);
      if (parsed && typeof parsed === 'object') {
        incoming = { chats: parsed.chats || {}, order: parsed.order || [] };
      }
    } catch { /* 损坏的快照直接忽略 */ }
    if (!incoming) return;
    const merged = mergeChatStores(st.store, incoming, { preferAllIds: st.sendingChatIds });
    useChatStore.setState({ store: merged, storeRef: merged });
  });
}
