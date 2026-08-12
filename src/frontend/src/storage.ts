import type { Catalog, ChatMessage, ChatStore } from './types';

export const STORAGE_KEY = 'hugagent_ui_chat_history_v2';
export const ENABLE_KEY = 'hugagent_ui_enabled_catalog_v1';

export const defaultCatalog: Catalog = {
  skills: [],
  agents: [],
  mcp: [],
  kb: [],
};

/** Debounce window for chat-store writes. Streaming pumps `updateStore` dozens
 *  of times per second; serializing the full chat tree synchronously each
 *  time blocks the main thread. We coalesce into one write per window. */
const SAVE_DEBOUNCE_MS = 800;

let pendingSaveTimer: number | null = null;
let pendingSavePayload: { userId: string; store: ChatStore } | null = null;

/** Append the user id to a base key so different accounts on the same browser
 *  don't share localStorage entries. Returns null when there is no user yet —
 *  callers must skip read/write in that case. */
export function userScopedKey(base: string, userId: string | null | undefined): string | null {
  if (!userId) return null;
  return `${base}:${userId}`;
}

/** One-time cleanup of the pre-userscoped global keys. Safe to call repeatedly. */
export function purgeLegacyUnscopedKeys() {
  if (typeof window === 'undefined') return;
  const legacyKeys = [
    STORAGE_KEY,
    'hugagent_current_chat_id',
    'hugagent_pending_scroll_message_ts',
    'hugagent_share_records_cache',
    'hugagent_automation_sidebar_prefs_v1',
  ];
  for (const k of legacyKeys) {
    try { window.localStorage.removeItem(k); } catch { /* ignore */ }
  }
}

export function loadChatStore(userId: string | null | undefined): ChatStore {
  const key = userScopedKey(STORAGE_KEY, userId);
  if (!key) return { chats: {}, order: [] };
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return { chats: {}, order: [] };
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return { chats: {}, order: [] };
    return {
      chats: parsed.chats || {},
      order: parsed.order || [],
    };
  } catch {
    return { chats: {}, order: [] };
  }
}

/** Strip `toolCall.output` from every message before persisting to
 *  localStorage. The full output lives in the backend `ChatMessage.tool_calls`
 *  JSONB column; on refresh / chat switch, `useChatInit`'s lazy-load (gated
 *  by `loadedMsgIds`) calls `/v1/chats/{cid}/messages` and overwrites the
 *  in-memory messages with the complete payload, restoring `output`.
 *
 *  Persisting `output` here would balloon localStorage with multi-MB tool
 *  results (evaluation reports / batch outputs / knowledge-base retrieval), force synchronous
 *  multi-MB `JSON.stringify` on every save, and—on 4GB-RAM machines—stall
 *  the main thread long enough to crash the tab.
 *
 *  This rebuilds the affected branches as fresh objects; the original
 *  in-memory store is untouched, so the active session keeps its full
 *  `output` references for rendering. */
function trimForPersistence(store: ChatStore): ChatStore {
  let storeMutated = false;
  const nextChats: ChatStore['chats'] = {};
  for (const [chatId, chat] of Object.entries(store.chats || {})) {
    let chatMutated = false;
    const sourceMessages: ChatMessage[] = chat?.messages || [];
    const messages: ChatMessage[] = sourceMessages.map((m) => {
      if (!Array.isArray(m.toolCalls) || m.toolCalls.length === 0) return m;
      let toolMutated = false;
      const toolCalls = m.toolCalls.map((tc) => {
        if (tc?.output === undefined) return tc;
        toolMutated = true;
        // Setting to undefined causes JSON.stringify to omit the key entirely,
        // matching the shape the loader expects (output as optional `any`).
        return { ...tc, output: undefined };
      });
      if (!toolMutated) return m;
      chatMutated = true;
      return { ...m, toolCalls };
    });
    if (chatMutated) {
      storeMutated = true;
      nextChats[chatId] = { ...chat, messages };
    } else {
      nextChats[chatId] = chat;
    }
  }
  return storeMutated ? { ...store, chats: nextChats } : store;
}

/** 本标签页本会话里删除过的 chat id：合并写盘时不许它们从磁盘"复活"。 */
const sessionDeletedChatIds = new Set<string>();

export function registerDeletedChatId(id: string) {
  sessionDeletedChatIds.add(id);
}

/** 由 chatStore 注册：返回本标签页正在流式输出的 chat id 集合。
 *  合并写盘时这些会话一律以本内存版本为准（磁盘上可能是别的标签页的旧影子）。 */
let streamingIdsProvider: (() => Set<string>) | null = null;
export function setStreamingIdsProvider(fn: () => Set<string>) {
  streamingIdsProvider = fn;
}

/**
 * 按会话粒度合并两棵聊天树：同一 chat 取 updatedAt 较新者（相同时取 preferred 侧）。
 * 过去写盘是"整棵树全量覆盖"——两个标签页各持一份旧快照互相抹掉对方的新消息/
 * 新会话，切标签页（visibilitychange→flush）就稳定触发（问题17 串台的主因之一）。
 */
export function mergeChatStores(
  preferred: ChatStore,
  other: ChatStore,
  opts?: { preferAllIds?: Set<string> },
): ChatStore {
  const chats: ChatStore['chats'] = {};
  const ids = new Set([...Object.keys(preferred.chats || {}), ...Object.keys(other.chats || {})]);
  for (const id of ids) {
    if (sessionDeletedChatIds.has(id)) continue;
    const a = preferred.chats[id];
    const b = other.chats[id];
    if (!a) { chats[id] = b; continue; }
    if (!b) { chats[id] = a; continue; }
    if (opts?.preferAllIds?.has(id)) { chats[id] = a; continue; }
    chats[id] = (b.updatedAt || 0) > (a.updatedAt || 0) ? b : a;
  }
  // order 语义 ≈ 最近使用顺序；合并后按 updatedAt 降序重建，
  // 原 order 中未知的 id（已被删除/过滤）自然剔除。
  const order = Object.values(chats)
    .sort((x, y) => (y.updatedAt || 0) - (x.updatedAt || 0))
    .map((c) => c.id);
  return { chats, order };
}

function performSave(userId: string, store: ChatStore) {
  const key = userScopedKey(STORAGE_KEY, userId);
  if (!key) return;
  try {
    // 合并写：先读磁盘上（可能来自其他标签页的）最新快照，按会话粒度合并后再写，
    // 本内存快照没动过的会话不会覆盖掉其他标签页刚写入的新内容。
    const disk = loadChatStore(userId);
    const merged = mergeChatStores(store, disk, {
      preferAllIds: streamingIdsProvider ? streamingIdsProvider() : undefined,
    });
    localStorage.setItem(key, JSON.stringify(trimForPersistence(merged)));
  } catch {
    // ignore quota errors
  }
}

/** Coalesce high-frequency writes into one localStorage.setItem per debounce
 *  window. The latest payload always wins — older snapshots are dropped. */
export function saveChatStoreDebounced(userId: string | null | undefined, store: ChatStore) {
  if (!userId) return;
  pendingSavePayload = { userId, store };
  if (pendingSaveTimer != null) return;
  pendingSaveTimer = window.setTimeout(() => {
    pendingSaveTimer = null;
    const payload = pendingSavePayload;
    pendingSavePayload = null;
    if (payload) performSave(payload.userId, payload.store);
  }, SAVE_DEBOUNCE_MS);
}

/** Force any queued debounced write to flush synchronously. Call before
 *  logout, user switch, or when the document is being hidden / unloaded. */
export function flushChatStore() {
  if (pendingSaveTimer != null) {
    clearTimeout(pendingSaveTimer);
    pendingSaveTimer = null;
  }
  const payload = pendingSavePayload;
  pendingSavePayload = null;
  if (payload) performSave(payload.userId, payload.store);
}

if (typeof window !== 'undefined') {
  // pagehide fires reliably on tab close / navigation in all browsers.
  window.addEventListener('pagehide', () => flushChatStore());
  // visibilitychange→hidden covers tab background / mobile app switch where
  // the OS may kill the page before pagehide arrives.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushChatStore();
  });
}

/**
 * 读写一个「JSON 对象」型 localStorage 偏好项。
 *
 * 这个 guard + try/JSON.parse + 类型兜底的组合此前在 catalogStore / uiStore /
 * automationChatStore 里各抄了一遍；新的偏好项直接用这两个函数，别再抄第五份。
 * 需要按账号隔离的数据请先用 {@link userScopedKey} 包一下 key。
 */
export function loadJsonPref<T extends object>(key: string, fallback: T): T {
  if (typeof window === 'undefined') return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === 'object' ? (parsed as T) : fallback;
  } catch {
    return fallback;
  }
}

export function saveJsonPref(key: string, value: object) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch { /* localStorage unavailable (private mode / quota) */ }
}

export function loadCatalog(): Catalog {
  try {
    const raw = localStorage.getItem(ENABLE_KEY);
    if (!raw) return structuredClone(defaultCatalog);
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return structuredClone(defaultCatalog);
    return {
      skills: Array.isArray(parsed.skills) ? parsed.skills : [],
      agents: Array.isArray(parsed.agents) ? parsed.agents : [],
      mcp: Array.isArray(parsed.mcp) ? parsed.mcp : [],
      kb: Array.isArray(parsed.kb) ? parsed.kb : [],
    };
  } catch {
    return structuredClone(defaultCatalog);
  }
}

export function saveCatalog(catalog: Catalog) {
  localStorage.setItem(ENABLE_KEY, JSON.stringify(catalog));
}

export function nowId(prefix = 'chat') {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const ts = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  // Random suffix: without it, two "new chats" created within the same second
  // collide on an identical id, and the backend merges their messages into one
  // conversation — causing cross-session content bleed.
  const rand = Math.random().toString(36).slice(2, 8);
  return `${prefix}_${ts}_${rand}`;
}
