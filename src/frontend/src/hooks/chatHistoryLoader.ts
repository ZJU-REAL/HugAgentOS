import { authFetch, chatTargetHeaders } from '../api';
import { useAuthStore, useChatStore } from '../stores';
import { markResolvedPlanPreviews, scanPlanSnapshots } from '../utils/planHistory';
import { mergeHistoryPage } from '../utils/historyMerge';
import { parseContextCompactionState, parseContextUsageSnapshot } from '../utils/contextUsage';
import { shouldRestorePlanModeFromHistory } from '../utils/chatMode';
import { parseHistoryMessage } from './chatHistoryMessage';
const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

// Chats with a message fetch currently in flight. Separate from
// `loadedMsgIds` on purpose: the store mark means "successfully loaded"
// (drives the skeleton-vs-empty-state UI), while this set is only the
// re-entrancy lock so concurrent effect runs / the startup preload don't
// double-fetch the same chat. Module-level is fine — it must survive
// re-renders but reset on page refresh, exactly like the store marks.
export const inflightMsgLoads = new Set<string>();

// Per-chat failed message-load attempts. A non-2xx / network failure used to
// leave the chat stuck on the skeleton until the user switched away and back;
// now we self-retry a few times with backoff (bumpSessionLoadEpoch re-fires
// the lazy-load effect), then give up until the next manual visit.
/** 打开会话先渲染多少条历史；往上滚续拉也按这个粒度。
 *
 *  过去是把每一页都拉完再一次性渲染整段历史——跑过大文件的长对话，光这一步就能
 *  把浏览器压垮。这里只铺第一屏，剩下的交给滚动续拉。 */
export const MESSAGE_PAGE_SIZE = 30;

export const msgLoadRetryCounts = new Map<string, number>();
export const MSG_LOAD_MAX_RETRIES = 3;

/**
 * 往上滚到顶时续拉更早的一页历史，拼回消息列表的最前面。
 *
 * 返回本次真正新增了多少条：0 表示已经到最早的一条、正在拉、或者这一页全是重复。
 * 去重按消息身份——滚动触发可能与其它路径的写入并发。
 */
export async function loadOlderMessages(chatId: string): Promise<number> {
  const userId = useAuthStore.getState().authUser?.user_id;
  const store = useChatStore.getState();
  const paging = store.messagePaging[chatId];
  if (!paging || !paging.hasOlder || paging.loading) return 0;
  store.setMessagePaging(chatId, { ...paging, loading: true });
  try {
    const r = await authFetch(
      `${effectiveApiUrl}/v1/chats/${chatId}/messages`
      + `?page=${paging.nextPage}&page_size=${MESSAGE_PAGE_SIZE}&order=desc`,
      { headers: { ...chatTargetHeaders(chatId) } },
    );
    if (!r.ok) throw new Error(`older messages: HTTP ${r.status}`);
    const payload = await r.json();
    if (useAuthStore.getState().authUser?.user_id !== userId) return 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const items: any[] = payload?.data?.items || [];
    const older = items.map(parseHistoryMessage);
    let added = 0;
    useChatStore.getState().updateStore((prev) => {
      const chat = prev.chats[chatId];
      if (!chat) return prev;
      const existing = chat.messages || [];
      const seen = new Set(existing.map((m) => m.uid));
      const fresh = older.filter((m) => !seen.has(m.uid));
      added = fresh.length;
      if (fresh.length === 0) return prev;
      return {
        ...prev,
        chats: {
          ...prev.chats,
          [chatId]: { ...chat, messages: markResolvedPlanPreviews([...fresh, ...existing]) },
        },
      };
    });
    useChatStore.getState().setMessagePaging(chatId, {
      nextPage: paging.nextPage + 1,
      hasOlder: !!payload?.data?.pagination?.has_next,
      loading: false,
    });
    return added;
  } catch {
    // 失败就把 loading 放掉，让下一次滚动重试；不改 nextPage，不丢游标。
    if (useAuthStore.getState().authUser?.user_id !== userId) return 0;
    const latest = useChatStore.getState().messagePaging[chatId];
    if (latest) useChatStore.getState().setMessagePaging(chatId, { ...latest, loading: false });
    return 0;
  }
}

const pendingReloads = new Map<string, Promise<boolean>>();

/**
 * 取这个会话的最近一页并并进本地。首屏加载、对账、重挂、回放收尾、后端自起轮次——
 * 所有需要历史的地方都走这里。服务端那一行从轮次接纳起就存在并持续刷新，所以这一页
 * 天然包含正在跑的那一轮。同一会话并发的请求合并成一次。
 */
export function reloadChatHistory(chatId: string): Promise<boolean> {
  const userId = useAuthStore.getState().authUser?.user_id;
  const key = JSON.stringify([userId, chatId, chatTargetHeaders(chatId)]);
  const pending = pendingReloads.get(key);
  if (pending) return pending;
  const task = fetchAndMergeHistoryPage(chatId, userId).finally(() => pendingReloads.delete(key));
  pendingReloads.set(key, task);
  return task;
}

async function fetchAndMergeHistoryPage(chatId: string, userId: string | undefined): Promise<boolean> {
  const r = await authFetch(
    `${effectiveApiUrl}/v1/chats/${chatId}/messages?page=1&page_size=${MESSAGE_PAGE_SIZE}&order=desc`,
    { headers: { ...chatTargetHeaders(chatId) } },
  );
  if (!r.ok) return false;
  const payload = await r.json();
  if (useAuthStore.getState().authUser?.user_id !== userId) return false;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const items: any[] = payload?.data?.items || [];
  const page = items.map(parseHistoryMessage);
  const { hasPlanMessages, pendingPlanId } = scanPlanSnapshots(items.filter((item) => item.metadata?.forked_history !== true));
  const st = useChatStore.getState();
  if (Object.prototype.hasOwnProperty.call(payload?.data || {}, 'context_usage')) {
    st.setContextUsage(chatId, parseContextUsageSnapshot(payload.data.context_usage));
  }
  if (Object.prototype.hasOwnProperty.call(payload?.data || {}, 'context_compaction')) {
    st.setContextCompaction(chatId, parseContextCompactionState(payload.data.context_compaction));
  }
  // 更早的页可能已经滚上去加载过了，游标只能前进不能回拨。
  st.setMessagePaging(chatId, {
    nextPage: Math.max(2, st.messagePaging[chatId]?.nextPage ?? 0),
    hasOlder: !!payload?.data?.pagination?.has_next,
    loading: false,
  });
  st.updateStore((prev) => {
    const c = prev.chats[chatId];
    if (!c) return prev;
    const merged = mergeHistoryPage(c.messages || [], page, {
      localIsWriter: useChatStore.getState().sendingChatIds.has(chatId),
    });
    return {
      ...prev,
      chats: {
        ...prev.chats,
        [chatId]: {
          ...c,
          messages: markResolvedPlanPreviews(merged),
          ...(hasPlanMessages && !c.planChat ? { planChat: true } : {}),
        },
      },
    };
  });
  st.addLoadedMsgId(chatId);
  const latest = useChatStore.getState();
  if (chatId !== latest.currentChatId) return true;
  if (hasPlanMessages && shouldRestorePlanModeFromHistory(latest.store.chats[chatId]) && !latest.planMode) {
    latest.setPlanMode(true);
  }
  if (pendingPlanId) latest.setCurrentPlanId(pendingPlanId);
  return true;
}

/**
 * 把这个会话剩下的历史全部补齐（一页一页续拉，直到没有更早的）。
 *
 * 常规浏览一律走"只铺第一屏 + 滚动续拉"，但有几件事**必须**看到整段历史：导出 PDF、
 * 计划模式把历史带给后端。这两处在动手前先调一次这里，别拿半截历史当全部。
 * 上限兜一道：真有异常长的会话也不至于在这儿转到天荒地老。
 */
export async function ensureFullMessages(chatId: string, maxPages = 100): Promise<void> {
  for (let i = 0; i < maxPages; i += 1) {
    const paging = useChatStore.getState().messagePaging[chatId];
    if (!paging?.hasOlder) return;
    const added = await loadOlderMessages(chatId);
    // 拉不动了（请求失败 / 整页都是重复）就停，避免空转。
    if (added === 0 && !useChatStore.getState().messagePaging[chatId]?.loading) return;
  }
}
