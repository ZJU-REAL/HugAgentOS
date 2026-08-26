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
    loadUnboundProjectIds(userId);
    return stripUnboundProjects({
      chats: parsed.chats || {},
      order: parsed.order || [],
    });
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

/** 已删除项目的 id。落 localStorage（按账号隔离）而不是只放内存：
 *  合并写盘是按 chat 的 updatedAt 取新者，而"解绑项目"并不改 updatedAt ——
 *  A 窗口删了项目、把 chat 的 projectId 摘掉写回磁盘后，还留着旧绑定的 B 窗口
 *  一写盘就按 updatedAt 平手把绑定又贴了回去。侧边栏于是拿 chat.projectName
 *  兜底造出一个"已删除项目"的分组，新对话就挂在一个并不存在的项目下。
 *  记成跨窗口可见的黑名单后，任何一侧合并时都会把这些绑定清干净。 */
const UNBOUND_PROJECTS_KEY = 'hugagent_ui_unbound_projects_v1';
const UNBOUND_PROJECTS_MAX = 200;
// 只保留"当前账号"的那一份：换账号时整体丢弃，别把 A 的已删项目带到 B 的会话上。
let unboundProjectOwner: string | null = null;
let unboundProjectIds = new Set<string>();

function unboundProjectsKey(userId: string | null | undefined): string | null {
  return userScopedKey(UNBOUND_PROJECTS_KEY, userId);
}

/** 载入该账号已登记的已删除项目（含其他窗口写入的）。 */
export function loadUnboundProjectIds(userId: string | null | undefined): Set<string> {
  const owner = userId || null;
  if (owner !== unboundProjectOwner) {
    unboundProjectOwner = owner;
    unboundProjectIds = new Set<string>();
  }
  const key = unboundProjectsKey(userId);
  if (!key || typeof window === 'undefined') return unboundProjectIds;
  try {
    const raw = window.localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : null;
    if (Array.isArray(parsed)) for (const id of parsed) if (typeof id === 'string') unboundProjectIds.add(id);
  } catch { /* ignore */ }
  return unboundProjectIds;
}

/** 项目被删除时登记：本窗口立即生效，其他窗口下次合并/加载时生效。 */
export function registerUnboundProject(userId: string | null | undefined, projectId: string) {
  if (!projectId) return;
  loadUnboundProjectIds(userId).add(projectId);
  const key = unboundProjectsKey(userId);
  if (!key || typeof window === 'undefined') return;
  try {
    const next = [...unboundProjectIds].slice(-UNBOUND_PROJECTS_MAX);
    window.localStorage.setItem(key, JSON.stringify(next));
  } catch { /* ignore */ }
}

/** 把已删除项目的绑定从聊天树里摘掉（不改 updatedAt，避免侧边栏顺序跳动）。 */
function stripUnboundProjects(store: ChatStore): ChatStore {
  if (unboundProjectIds.size === 0) return store;
  let mutated = false;
  const chats: ChatStore['chats'] = {};
  for (const [id, chat] of Object.entries(store.chats || {})) {
    if (chat?.projectId && unboundProjectIds.has(chat.projectId)) {
      const next = { ...chat };
      delete next.projectId;
      delete next.projectName;
      chats[id] = next;
      mutated = true;
    } else {
      chats[id] = chat;
    }
  }
  return mutated ? { ...store, chats } : store;
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
  return stripUnboundProjects({ chats, order });
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

/** 订阅"另一个窗口改了这个账号的聊天树"。
 *
 *  多开窗口时每个页面各持一份内存快照，磁盘只在写盘时按会话粒度合并 ——
 *  所以 A 窗口新建/改名/解绑的会话，B 窗口在刷新之前一直看不到，反过来 B 窗口
 *  随后的一次写盘还会把自己的旧快照贴回磁盘。用户看到的就是"另开一个窗口就冒出
 *  一堆记录 / 对话挂在已删除的项目下，刷新后才正常"。这里监听 storage 事件把
 *  外部改动即时合回内存，两个窗口不再各说各话。
 *
 *  只读不写：回调里不得再触发写盘，否则两个窗口会互相唤醒形成写盘乒乓。 */
export function subscribeChatStoreChanges(
  userId: string | null | undefined,
  handler: (disk: ChatStore) => void,
): () => void {
  const key = userScopedKey(STORAGE_KEY, userId);
  if (!key || typeof window === 'undefined') return () => { /* noop */ };
  const onStorage = (e: StorageEvent) => {
    if (e.key !== key) return;
    handler(loadChatStore(userId));
  };
  window.addEventListener('storage', onStorage);
  return () => window.removeEventListener('storage', onStorage);
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
