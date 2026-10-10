import { saveChatStoreDebounced } from '../storage';
import type { ChatStore as ChatStoreData } from '../types';
import { resolveModeSlug, resolvePlanModeActive } from '../utils/chatMode';
import { savePendingScrollMessageTs, restoredEffort } from './chatSupport';
import type { ChatState, ThinkingEffort } from './chatState';
import type { ChatActionsContext } from './chatSupport';



export function createChatSelectionActions({ set, get, initializeDraftRunTarget, syncChatUrl, publishCurrentChatUrl }: ChatActionsContext): Pick<ChatState,
  'setStore' | 'updateStore' | 'setCurrentChatId' | 'syncCurrentChatMode' | 'adoptChatFromUrl' | 'toggleThinking' | 'setChatMode' | 'setModeSlug' | 'setToolResultPanel' | 'setCopiedMsg' | 'setChatsLoading' | 'setFeedbackMap' | 'setDislikingUid' | 'setDislikeComment' | 'setToolDisplayNames' | 'addBackendSessionId' | 'removeBackendSessionId' | 'clearBackendSessionIds' | 'addLoadedMsgId' | 'removeLoadedMsgId' | 'clearLoadedMsgIds' | 'setMessagePaging' | 'applyToolCallOutput' | 'setShareSelectionMode' | 'toggleShareMessageUid' | 'clearShareSelection' | 'startShareSelectionWithAll' | 'setPendingScrollMessageTs' | 'setPlanMode' | 'setLoopMode' | 'setCurrentPlanId' | 'setEditingMessageUid' | 'bumpSessionLoadEpoch'
> {
  return {
    setStore: (store) => {
        set({ store, storeRef: store });
        saveChatStoreDebounced(get().currentUserId, store);
        initializeDraftRunTarget(get().currentChatId);
        publishCurrentChatUrl();
      },
    updateStore: (updater) => {
        const next = updater(get().store);
        set({ store: next, storeRef: next });
        saveChatStoreDebounced(get().currentUserId, next);
        initializeDraftRunTarget(get().currentChatId);
        publishCurrentChatUrl();
      },
    setCurrentChatId: (id) => {
        get().adoptChatFromUrl(id);
        syncChatUrl(id);
      },
    syncCurrentChatMode: () => {
        const chat = get().store.chats[get().currentChatId];
        set({ planMode: resolvePlanModeActive(chat), modeSlug: resolveModeSlug(chat), ...restoredEffort(chat) });
      },
    adoptChatFromUrl: (id) => {
        initializeDraftRunTarget(id);
        const chat = get().store.chats[id];
        set({
          currentChatId: id,
          sending: get().sendingChatIds.has(id),
          planMode: resolvePlanModeActive(chat),
          // 模式跟着对话走：切到哪段对话就恢复它记录上的模式，没记录的按标准模式。
          modeSlug: resolveModeSlug(chat),
          // 思考强度与待发送引用同理随对话记录恢复；强度没记录时沿用当前档位。
          ...restoredEffort(chat),
          // Autonomous loop is a "one-shot composer intent" and does not persist with the chat —
          // switching to any chat resets to a normal conversation, so a loop mode left on in the
          // previous chat doesn't carry over into a new/other chat.
          loopMode: false,
          currentPlanId: null,
        });
      },
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
    setCopiedMsg: (uid) => set({ copiedMsg: uid }),
    setChatsLoading: (v) => set({ chatsLoading: v }),
    setFeedbackMap: (map) => set({ feedbackMap: map }),
    setDislikingUid: (uid) => set({ dislikingUid: uid }),
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
    setMessagePaging: (chatId, paging) => set((s) => {
        const next = { ...s.messagePaging };
        if (paging) next[chatId] = paging;
        else delete next[chatId];
        return { messagePaging: next };
      }),
    applyToolCallOutput: (chatId, messageId, toolId, output) => set((s) => {
        const chat = s.store.chats[chatId];
        if (!chat) return {};
        let touched = false;
        const messages = (chat.messages || []).map((m) => {
          if (m.messageId !== messageId || !Array.isArray(m.toolCalls)) return m;
          const toolCalls = m.toolCalls.map((tc) => {
            if (tc.id !== toolId) return tc;
            touched = true;
            // outputLoaded 一旦置上就不再回退：同一张卡展开/收起多次只取一次全文。
            return { ...tc, output, outputTruncated: false, outputLoaded: true };
          });
          return touched ? { ...m, toolCalls } : m;
        });
        if (!touched) return {};
        const store = {
          ...s.store,
          chats: { ...s.store.chats, [chatId]: { ...chat, messages } },
        };
        return { store, storeRef: store };
      }),
    setShareSelectionMode: (v) => set((s) => ({
        shareSelectionMode: v,
        selectedShareMessageUids: v ? s.selectedShareMessageUids : new Set(),
      })),
    toggleShareMessageUid: (uid) => set((s) => {
        const next = new Set(s.selectedShareMessageUids);
        if (next.has(uid)) next.delete(uid); else next.add(uid);
        return { selectedShareMessageUids: next };
      }),
    clearShareSelection: () => set({ shareSelectionMode: false, selectedShareMessageUids: new Set() }),
    startShareSelectionWithAll: (uidList) => set({
        shareSelectionMode: true,
        selectedShareMessageUids: new Set(uidList),
      }),
    setPendingScrollMessageTs: (ts) => {
        savePendingScrollMessageTs(get().currentUserId, ts);
        set({ pendingScrollMessageTs: ts });
      },
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
    setCurrentPlanId: (id) => set({ currentPlanId: id }),
    setEditingMessageUid: (uid) => set({ editingMessageUid: uid }),
    bumpSessionLoadEpoch: () => set((s) => ({ sessionLoadEpoch: s.sessionLoadEpoch + 1 }))
  };
}
