import { listSidebarChatRuns } from '../sidebarStatusApi';
import { normalizeTaskStatus, useTaskNotificationStore } from './taskNotificationStore';
import { isOlderContextUsage } from '../utils/contextUsageSnapshot';
import { saveChatStoreDebounced } from '../storage';
import type { ChatStore as ChatStoreData } from '../types';
import { saveQueuedMessages } from './chatSupport';
import type { ChatState } from './chatState';
import type { ChatActionsContext } from './chatSupport';

// A run that starts while a check is in flight still falls inside the next window.
const DISCOVERY_OVERLAP_S = 5;
// Per account on this page: when the last check started, and chats already looked up once
// (an unreadable one is not fetched again on every tick).
let discovery: { owner: string | null; since: number; seen: Set<string> } = { owner: null, since: Date.now(), seen: new Set() };
let inflight: Promise<void> | null = null;

export function createChatRunActions({ set, get }: ChatActionsContext): Pick<ChatState,
  'setSending' | 'addSendingChatId' | 'removeSendingChatId' | 'refreshRemoteRunningChats' | 'setActiveRun' | 'clearActiveRun' | 'setQueuedMessage' | 'updateQueuedMessage' | 'setVisionReading' | 'setCompactionNotice' | 'dismissCompactionNotice' | 'setContextCompaction' | 'setContextUsage' | 'truncateMessagesFrom' | 'setPlanProgress'
> {

  async function refreshRunning() {
    const { getSession, registerLocalChat } = await import('../api');
    const owner = get().currentUserId;
    if (discovery.owner !== owner) discovery = { owner, since: Date.now(), seen: new Set() };
    // While the session list loads, every chat would look unknown.
    const discover = !get().chatsLoading;
    const started = Date.now();
    const windowSeconds = discover ? Math.ceil((started - discovery.since) / 1000) + DISCOVERY_OVERLAP_S : 0;
    const scope = useTaskNotificationStore.getState().scope;
    const { items, recent, statuses } = await listSidebarChatRuns(windowSeconds);
    if (get().currentUserId !== owner || useTaskNotificationStore.getState().scope !== scope) return;
    useTaskNotificationStore.getState().observe(statuses.map(row => ({
      key: 'chat:' + row.chat_id, id: row.run_id, messageId: row.message_id,
      status: normalizeTaskStatus(row.status), startedAt: Date.parse(row.created_at), updatedAt: Date.parse(row.updated_at),
    })));
    const running = new Set(items.map((item) => item.chat_id));
    const lit = get().remoteRunningChatIds;
    if (running.size !== lit.size || [...running].some((id) => !lit.has(id))) set({ remoteRunningChatIds: running });
    if (!discover || get().currentUserId !== owner) return;
    discovery.since = started;
    // A chat that the Personal Agent, the API or another device started has a run but no
    // sidebar entry here. Every interesting chat starts a run, so runs are the signal:
    // the live ones, and the ones that started and ended since the last check.
    const missing = new Map<string, 'local' | undefined>();
    for (const { chat_id, target } of [...items, ...recent]) {
      if (!get().store.chats[chat_id] && !discovery.seen.has(chat_id)) missing.set(chat_id, target);
    }
    if (!missing.size) return;
    for (const [id, target] of missing) {
      discovery.seen.add(id);
      if (target === 'local') registerLocalChat(id);
    }
    const results = await Promise.allSettled([...missing.keys()].map((id) => getSession(id)));
    if (get().currentUserId !== owner) return;
    const added = results
      .flatMap((result) => (result.status === 'fulfilled' ? [result.value] : []))
      .filter((session) => !get().store.chats[session.id])
      .map((session) => (missing.get(session.id) ? { ...session, runTarget: 'local' as const } : session));
    if (!added.length) return;
    get().updateStore((prev) => ({
      ...prev,
      chats: { ...prev.chats, ...Object.fromEntries(added.map((session) => [session.id, session])) },
      order: [...added.map((session) => session.id), ...prev.order.filter((id) => !missing.has(id))],
    }));
    set((s) => ({ backendSessionIds: new Set([...s.backendSessionIds, ...added.map((session) => session.id)]) }));
  }

  return {
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
    refreshRemoteRunningChats: () => {
        // Switching back to a tab fires both focus and visibilitychange: share one request.
        inflight ??= refreshRunning().finally(() => { inflight = null; });
        return inflight;
      },
    setActiveRun: (chatId, info) => set((s) => ({
        activeRuns: { ...s.activeRuns, [chatId]: info },
      })),
    clearActiveRun: (chatId) => set((s) => {
        const next = { ...s.activeRuns };
        delete next[chatId];
        // 本标签页刚确知这一轮结束了，比服务端快照新——立刻灭灯，不等下一次刷新。
        if (!s.remoteRunningChatIds.has(chatId)) return { activeRuns: next };
        const remote = new Set(s.remoteRunningChatIds);
        remote.delete(chatId);
        return { activeRuns: next, remoteRunningChatIds: remote };
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
        const previous = s.contextCompactions[chatId];
        if (state && previous && state.checkpointTs < previous.checkpointTs) return {};
        if (!state?.contextUsage) return { contextCompactions: next };
        if (isOlderContextUsage(state.contextUsage, s.contextUsages[chatId])) {
          return { contextCompactions: next };
        }
        return {
          contextCompactions: next,
          contextUsages: { ...s.contextUsages, [chatId]: state.contextUsage },
        };
      }),
    setContextUsage: (chatId, state) => set((s) => {
        if (state && isOlderContextUsage(state, s.contextUsages[chatId])) return {};
        const next = { ...s.contextUsages };
        if (state) next[chatId] = state;
        else delete next[chatId];
        return { contextUsages: next };
      }),
    truncateMessagesFrom: (chatId, anchor) => {
        const { store } = get();
        const chat = store.chats[chatId];
        if (!chat) return;
        const idx = chat.messages.findIndex((m) => m.uid === anchor.uid);
        // 锚点是从渲染出来的列表里取的，找不到说明它已经不在了——什么都不该删。
        if (idx < 0) return;
        const filtered = chat.messages.slice(0, idx);
        const next: ChatStoreData = {
          ...store,
          chats: { ...store.chats, [chatId]: { ...chat, messages: filtered, updatedAt: Date.now() } },
        };
        const nextUsages = { ...get().contextUsages };
        delete nextUsages[chatId];
        const nextCompactions = { ...get().contextCompactions };
        delete nextCompactions[chatId];
        set({
          store: next, storeRef: next, contextUsages: nextUsages, contextCompactions: nextCompactions,
          contextUsageEpochs: { ...get().contextUsageEpochs, [chatId]: (get().contextUsageEpochs[chatId] || 0) + 1 },
        });
        saveChatStoreDebounced(get().currentUserId, next);
      },
    setPlanProgress: (chatId, p) => set((s) => ({
        planProgress: { ...s.planProgress, [chatId]: p },
      }))
  };
}
