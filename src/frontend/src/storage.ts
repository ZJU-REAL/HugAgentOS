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

function performSave(userId: string, store: ChatStore) {
  const key = userScopedKey(STORAGE_KEY, userId);
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify(trimForPersistence(store)));
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
