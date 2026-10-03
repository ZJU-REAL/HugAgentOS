import { saveChatStoreDebounced } from '../storage';
import type { ChatStore as ChatStoreData } from '../types';
import { saveQueuedMessages } from './chatSupport';
import type { ChatState } from './chatState';
import type { ChatActionsContext } from './chatSupport';

export function createChatRunActions({ set, get }: ChatActionsContext): Pick<ChatState,
  'setSending' | 'addSendingChatId' | 'removeSendingChatId' | 'refreshRemoteRunningChats' | 'setActiveRun' | 'clearActiveRun' | 'setQueuedMessage' | 'updateQueuedMessage' | 'setVisionReading' | 'setCompactionNotice' | 'dismissCompactionNotice' | 'setContextCompaction' | 'setContextUsage' | 'truncateMessagesFrom' | 'setPlanProgress'
> {
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
    refreshRemoteRunningChats: async () => {
        const { listActiveChatRuns } = await import('../api');
        const items = await listActiveChatRuns();
        set({ remoteRunningChatIds: new Set(items.map((item) => item.chat_id)) });
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
        if (!state?.contextUsage) return { contextCompactions: next };
        return {
          contextCompactions: next,
          contextUsages: { ...s.contextUsages, [chatId]: state.contextUsage },
        };
      }),
    setContextUsage: (chatId, state) => set((s) => {
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
        set({ store: next, storeRef: next, contextUsages: nextUsages });
        saveChatStoreDebounced(get().currentUserId, next);
      },
    setPlanProgress: (chatId, p) => set((s) => ({
        planProgress: { ...s.planProgress, [chatId]: p },
      }))
  };
}
