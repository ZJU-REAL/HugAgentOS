import { saveChatStoreDebounced } from '../storage';
import type { ChatStore as ChatStoreData } from '../types';
import type { ChatState } from './chatState';
import type { ChatActionsContext } from './chatSupport';
import { isLocalProject } from '../api';
import { t } from '../i18n';
import type { ChatItem } from '../types';

export function createChatProjectActions({ set, get }: ChatActionsContext): Pick<ChatState,
  'bindChatProject' | 'setChatRunTarget' | 'unbindChatProject'
> {
  return {
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
        // An explicit project choice also determines the draft's execution target.
        if (isLocalProject(projectId)) nextChat.runTarget = 'local';
        else nextChat.runTarget = 'cloud';
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
        else nextChat.runTarget = 'cloud';
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
      }
  };
}
