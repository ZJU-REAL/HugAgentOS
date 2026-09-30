import { message, Modal } from 'antd';
import { deleteSession } from '../api';
import { t } from '../i18n';
import { navigateTo, pathForPanel } from '../routing/navigation';
import { isAutomationHistoryChat } from './history';
import { newDraftChatId } from '../storage';
import { useAutomationChatStore } from '../stores/automationChatStore';
import { useChatStore } from '../stores/chatStore';

/** Shared deletion for ordinary conversations and scheduled-task conversations. */
export function confirmDeleteChat(id: string) {
  Modal.confirm({
    title: t('删除历史对话'),
    content: t('确定删除该历史对话吗？该操作不可恢复。'),
    okText: t('删除'),
    okButtonProps: { danger: true },
    cancelText: t('取消'),
    onOk: async () => {
      try {
        if (useChatStore.getState().backendSessionIds.has(id)) await deleteSession(id);
      } catch (error) {
        message.error(error instanceof Error ? error.message : t('删除失败'));
        throw error;
      }
      const chat = useChatStore.getState();
      if (chat.currentChatId === id && isAutomationHistoryChat(chat.store.chats[id])) {
        // Adopt without navigating: deleting the active task conversation stays in Scheduled Tasks.
        const nextId = chat.store.order.find(candidate => candidate !== id)
          || newDraftChatId(chat.currentUserId);
        chat.adoptChatFromUrl(nextId);
        chat.setInput('');
        useAutomationChatStore.getState().exitAutomationChat();
        navigateTo(pathForPanel('automation'), { replace: true });
      }
      chat.removeBackendSessionId(id);
      chat.removeLoadedMsgId(id);
      chat.deleteChat(id);
    },
  });
}
