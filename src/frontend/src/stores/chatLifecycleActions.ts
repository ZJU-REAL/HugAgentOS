import { saveChatStoreDebounced } from '../storage';
import type { ChatStore as ChatStoreData } from '../types';
import { resolveModeSlug, resolvePlanModeActive } from '../utils/chatMode';
import { saveQueuedMessages, restoredEffort } from './chatSupport';
import type { ChatState } from './chatState';
import type { ChatActionsContext } from './chatSupport';
import { chatDraftKey, useComposerStore } from './composerStore';
import { isDraftChatId, newDraftChatId } from '../storage';
import { chatIdFromPath } from '../routing/navigation';
import { flushChatStore, loadChatStore, mergeChatStores, nowId, purgeLegacyUnscopedKeys, registerDeletedChatId, subscribeChatStoreChanges } from '../storage';
import { applyDefaultChatMode, loadPendingScrollMessageTs, loadQueuedMessages } from './chatSupport';

let unsubscribeExternalChanges: (() => void) | null = null;

export function createChatLifecycleActions({ set, get, initializeDraftRunTarget, syncChatUrl }: ChatActionsContext): Pick<ChatState,
  'newChat' | 'deleteChat' | 'updateMessages' | 'currentChat' | 'hydrateForUser' | 'clearForLogout' | 'resumeHomeChat'
> {
  return {
    newChat: () => {
        const id = newDraftChatId(get().currentUserId);
        syncChatUrl(id);
        set({
          currentChatId: id,
          homeDraftId: id,
          sending: false,
          expandedThinking: new Set(),
          shareSelectionMode: false,
          selectedShareMessageUids: new Set(),
          planMode: false,
          currentPlanId: null,
          loopMode: false,
          ...applyDefaultChatMode(),
          modeSlug: 'standard',
        });
        initializeDraftRunTarget(id);
      },
    deleteChat: (id) => {
        const { store, currentChatId, currentUserId } = get();
        registerDeletedChatId(id);
        useComposerStore.getState().remove(chatDraftKey(id));
        const rest = { ...store.chats };
        delete rest[id];
        const nextCompactions = { ...get().contextCompactions };
        const nextUsages = { ...get().contextUsages };
        const nextNotices = { ...get().compactionNotices };
        const nextQueued = { ...get().queuedMessages };
        delete nextCompactions[id];
        delete nextUsages[id];
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
          contextUsages: nextUsages,
          compactionNotices: nextNotices,
          queuedMessages: nextQueued,
        });
        saveChatStoreDebounced(currentUserId, next);
        saveQueuedMessages(currentUserId, nextQueued);
        if (currentChatId === id) {
          const newId = next.order[0] || newDraftChatId(currentUserId);
          const nextChat = next.chats[newId];
          syncChatUrl(newId);
          set({
            currentChatId: newId,
            planMode: resolvePlanModeActive(nextChat),
            modeSlug: resolveModeSlug(nextChat),
            ...restoredEffort(nextChat),
            loopMode: false,
            currentPlanId: null,
            shareSelectionMode: false,
            selectedShareMessageUids: new Set(),
          });
        }
        initializeDraftRunTarget(get().currentChatId);
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
        // 「当前开着哪段会话」的真源是地址栏；地址上没有会话 id（首页 `/`）就是一段新草稿
        const currentChatId = chatIdFromPath() || newDraftChatId(userId);
        const pendingScroll = loadPendingScrollMessageTs(userId);
        set({
          currentUserId: userId,
          homeDraftId: isDraftChatId(userId, currentChatId) ? currentChatId : null,
          store,
          storeRef: store,
          currentChatId,
          pendingScrollMessageTs: pendingScroll,
          // Reset any in-flight UI state carried over from a previous user.
          sendingChatIds: new Set(),
          remoteRunningChatIds: new Set(),
          backendSessionIds: new Set(),
          loadedMsgIds: new Set(),
      messagePaging: {},
          shareSelectionMode: false,
          selectedShareMessageUids: new Set(),
          planMode: resolvePlanModeActive(store.chats[currentChatId]),
          loopMode: false,
          currentPlanId: null,
          editingMessageUid: null,
          activeRuns: {},
          // Durable cards retain targetRunId and are reconciled, never blindly replayed.
          queuedMessages: loadQueuedMessages(userId),
          compactionNotices: {},
          contextCompactions: {},
          contextUsages: {},
          ...applyDefaultChatMode(),
          // 刷新/重新进入时恢复当前对话记录上的模式与思考强度，不再一律掉回默认。
          modeSlug: resolveModeSlug(store.chats[currentChatId]),
          ...restoredEffort(store.chats[currentChatId]),
        });
        initializeDraftRunTarget(currentChatId);
        // 多开窗口：另一个窗口改了这个账号的聊天树时，把外部改动即时合回内存，
        // 免得两个窗口各说各话（新建的会话看不见、已解绑的项目又被贴回来），
        // 非要刷新才对得上。只读不写，避免两个窗口互相唤醒写盘。
        unsubscribeExternalChanges?.();
        unsubscribeExternalChanges = subscribeChatStoreChanges(userId, (disk) => {
          const merged = mergeChatStores(get().store, disk, { preferAllIds: get().sendingChatIds });
          set({ store: merged, storeRef: merged });
        });
      },
    clearForLogout: () => {
        // Persist any debounced writes before tearing down — the user's most
        // recent messages must hit disk so they resume correctly on next login.
        flushChatStore();
        unsubscribeExternalChanges?.();
        unsubscribeExternalChanges = null;
        // Per-user keys stay on disk so the same user can resume later. We only
        // wipe in-memory state here so the new user (or login screen) never sees
        // the previous user's chats.
        set({
          currentUserId: null,
          homeDraftId: null,
          store: { chats: {}, order: [] },
          storeRef: { chats: {}, order: [] },
          currentChatId: nowId('chat'),
          sending: false,
          sendingChatIds: new Set(),
          remoteRunningChatIds: new Set(),
          backendSessionIds: new Set(),
          loadedMsgIds: new Set(),
      messagePaging: {},
          shareSelectionMode: false,
          selectedShareMessageUids: new Set(),
          pendingScrollMessageTs: null,
          planMode: false,
          loopMode: false,
          currentPlanId: null,
          editingMessageUid: null,
          activeRuns: {},
          queuedMessages: {},
          compactionNotices: {},
          contextCompactions: {},
          contextUsages: {},
        });
      },
    resumeHomeChat: () => {
        const id = get().homeDraftId;
        if (id && isDraftChatId(get().currentUserId, id) && !get().backendSessionIds.has(id) && !(get().store.chats[id]?.messages.length)) get().adoptChatFromUrl(id);
        else get().newChat();
      }
  };
}
